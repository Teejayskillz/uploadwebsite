"""
HMAC authentication for the blog -> uploader API (brief section 15).

Scheme
------
Every request carries::

    X-NZD-Key:        the client's key_id
    X-NZD-Timestamp:  unix seconds
    X-NZD-Nonce:      random, single-use
    X-NZD-Signature:  hex HMAC-SHA256

The signed string is::

    METHOD \\n PATH \\n QUERY \\n SHA256(body) \\n TIMESTAMP \\n NONCE

so a signature cannot be moved between endpoints, replayed, or reused with a
different body. Replay is blocked by the ``ApiNonce`` table, and clock skew is
bounded by ``MAX_SKEW_SECONDS``.

Why ``ApiClient.secret_hash`` is the MAC key
--------------------------------------------
Verifying an HMAC requires the same secret material the client holds, so storing
a one-way hash of a *password* is not possible here. The column stores
``sha256(shared_secret)`` and **both sides derive the key the same way**, so the
raw shared secret is never stored and the column is exactly the signing key.
Treat a database dump as equivalent to leaking the key, and rotate accordingly.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from uploader.models import ApiClient, ApiNonce

SIGNATURE_HEADER = "HTTP_X_NZD_SIGNATURE"
KEY_HEADER = "HTTP_X_NZD_KEY"
TIMESTAMP_HEADER = "HTTP_X_NZD_TIMESTAMP"
NONCE_HEADER = "HTTP_X_NZD_NONCE"

MAX_SKEW_SECONDS = 300
NONCE_TTL_SECONDS = 600


class ApiAuthError(Exception):
    """Authentication/authorisation failure. ``code`` is safe to log and return."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class AuthenticatedCaller:
    client: ApiClient
    scope: str


def derive_key(shared_secret: str) -> str:
    """The value both sides use as the HMAC key, and what the DB stores."""
    return hashlib.sha256((shared_secret or "").encode("utf-8")).hexdigest()


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def build_signature(
    *, key: str, method: str, path: str, query: str, body: bytes, timestamp: str, nonce: str
) -> str:
    """Canonical signature. Exposed so the blog-side client can reuse it exactly."""
    canonical = "\n".join(
        [
            method.upper(),
            path or "/",
            query or "",
            body_digest(body),
            str(timestamp),
            str(nonce),
        ]
    )
    return hmac.new(
        key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _header(request, name: str) -> str:
    return (request.META.get(name) or "").strip()


def _client_for(key_id: str) -> ApiClient:
    if not key_id:
        raise ApiAuthError("missing_key", "X-NZD-Key header is required.")
    try:
        client = ApiClient.objects.get(key_id=key_id)
    except ApiClient.DoesNotExist:
        raise ApiAuthError("unknown_key", "Unknown API key.") from None
    if not client.is_active:
        raise ApiAuthError("inactive_key", "This API key is disabled.")
    return client


def _check_timestamp(raw: str) -> None:
    try:
        sent = int(raw)
    except (TypeError, ValueError):
        raise ApiAuthError("bad_timestamp", "X-NZD-Timestamp must be unix seconds.") from None
    skew = abs(int(time.time()) - sent)
    if skew > MAX_SKEW_SECONDS:
        raise ApiAuthError("stale_timestamp", f"Clock skew of {skew}s exceeds the limit.")


def _consume_nonce(client: ApiClient, nonce: str) -> None:
    """Insert the nonce; a duplicate means the request is a replay."""
    if not nonce:
        raise ApiAuthError("missing_nonce", "X-NZD-Nonce header is required.")
    if len(nonce) > 64:
        raise ApiAuthError("bad_nonce", "Nonce is too long.")
    try:
        with transaction.atomic():
            ApiNonce.objects.create(
                key_id=client.key_id,
                nonce=nonce,
                expires_at=timezone.now() + timedelta(seconds=NONCE_TTL_SECONDS),
            )
    except IntegrityError:
        raise ApiAuthError("replayed_nonce", "This nonce has already been used.") from None


def authenticate(request, *, required_scope: str | None = None) -> AuthenticatedCaller:
    """
    Validate a signed request and return the caller.

    Raises :class:`ApiAuthError` on any failure. Callers must run this **before**
    touching any object referenced by the request.
    """
    client = _client_for(_header(request, KEY_HEADER))
    _check_timestamp(_header(request, TIMESTAMP_HEADER))

    nonce = _header(request, NONCE_HEADER)
    provided = _header(request, SIGNATURE_HEADER)
    if not provided:
        raise ApiAuthError("missing_signature", "X-NZD-Signature header is required.")

    expected = build_signature(
        key=client.secret_hash,
        method=request.method,
        path=request.path,
        query=request.META.get("QUERY_STRING", ""),
        body=request.body,
        timestamp=_header(request, TIMESTAMP_HEADER),
        nonce=nonce,
    )
    if not hmac.compare_digest(expected, provided):
        raise ApiAuthError("bad_signature", "Signature does not match the request.")

    # Only after the signature is proven do we spend a nonce row.
    _consume_nonce(client, nonce)

    if required_scope and not client.allows(required_scope):
        raise ApiAuthError("forbidden_scope", f"This key may not perform '{required_scope}'.")

    ApiClient.objects.filter(pk=client.pk).update(
        last_used_at=timezone.now(), request_count=F("request_count") + 1
    )
    return AuthenticatedCaller(client=client, scope=required_scope or "")


def new_nonce() -> str:
    """Fresh nonce for a client implementation (and for tests)."""
    return secrets.token_hex(16)


def signing_headers(
    *, key_id: str, key: str, method: str, path: str, query: str = "", body: bytes = b""
) -> dict[str, str]:
    """Build the four headers a caller must send. Used by tests and the blog client."""
    timestamp = str(int(time.time()))
    nonce = new_nonce()
    signature = build_signature(
        key=key, method=method, path=path, query=query, body=body, timestamp=timestamp, nonce=nonce
    )
    return {
        "HTTP_X_NZD_KEY": key_id,
        "HTTP_X_NZD_TIMESTAMP": timestamp,
        "HTTP_X_NZD_NONCE": nonce,
        "HTTP_X_NZD_SIGNATURE": signature,
    }

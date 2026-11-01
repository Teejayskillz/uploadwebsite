"""
Premium detection on the storage side (brief section 12).

The blog is the only authority on membership. It proves a visitor is premium by
attaching a **short-lived signed handoff token** to the download-page link:

    https://my.nzdworld.com/d/<video>/?u=<handoff>

This service verifies the signature with the shared secret and the token's own
expiry (`max_age`), then stores the result in its own host-only cookie so the
premium state survives the download session without being re-verified on every
request. Nothing the browser sends is trusted unless it is signed by the blog,
and the failure mode is **closed**: an invalid token means "free user", never
"premium".

The cookie is signed too, so it cannot be forged or edited into a premium badge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

from uploader.models import PremiumAssertion

HANDOFF_SALT = "nzdworld.premium.handoff.v2"
COOKIE_SALT = "nzdworld.premium.cookie.v2"
COOKIE_NAME = "nzdv_premium"


class PremiumError(Exception):
    pass


@dataclass(frozen=True)
class PremiumState:
    is_premium: bool
    subject: str = ""
    source: str = "guest"

    def __bool__(self) -> bool:  # pragma: no cover - sugar
        return self.is_premium


ANONYMOUS = PremiumState(is_premium=False, subject="", source="guest")


def _shared_secret() -> str:
    secret = str(getattr(settings, "API_SHARED_SECRET", "") or "")
    if not secret:
        # Falling back to SECRET_KEY would silently accept nothing and reject
        # everything; be explicit instead.
        return ""
    return secret


def handoff_max_age() -> int:
    return max(30, int(getattr(settings, "PREMIUM_HANDOFF_MAX_AGE_SECONDS", 300)))


def cookie_ttl() -> int:
    return max(60, int(getattr(settings, "PREMIUM_SESSION_TTL_SECONDS", 1800)))


def verify_handoff(token: str) -> PremiumState:
    """
    Validate a blog-issued handoff token.

    Any problem (missing secret, bad signature, expired, premium flag false)
    resolves to a free/guest state. There is no path where an invalid token
    grants premium.
    """
    secret = _shared_secret()
    if not token or not secret:
        return ANONYMOUS
    try:
        payload = signing.loads(token, salt=HANDOFF_SALT, key=secret, max_age=handoff_max_age())
    except (signing.BadSignature, signing.SignatureExpired, ValueError):
        return ANONYMOUS
    if not isinstance(payload, dict):
        return ANONYMOUS
    if not bool(payload.get("prem")):
        return ANONYMOUS
    subject = str(payload.get("sub") or "")[:150]
    if not subject:
        return ANONYMOUS
    record_assertion(subject, True)
    return PremiumState(is_premium=True, subject=subject, source="handoff")


def issue_cookie_value(subject: str) -> str:
    secret = _shared_secret()
    if not secret:
        raise PremiumError("API_SHARED_SECRET is not configured.")
    return signing.dumps(
        {"sub": subject[:150], "iat": int(timezone.now().timestamp())},
        salt=COOKIE_SALT,
        key=secret,
        compress=True,
    )


def read_cookie(request) -> PremiumState:
    secret = _shared_secret()
    if not secret:
        return ANONYMOUS
    token = request.COOKIES.get(COOKIE_NAME, "")
    if not token:
        return ANONYMOUS
    try:
        payload = signing.loads(token, salt=COOKIE_SALT, key=secret, max_age=cookie_ttl())
    except (signing.BadSignature, signing.SignatureExpired, ValueError):
        return ANONYMOUS
    subject = str((payload or {}).get("sub") or "")[:150]
    if not subject:
        return ANONYMOUS
    return PremiumState(is_premium=True, subject=subject, source="cookie")


def resolve_premium(request) -> PremiumState:
    """
    Premium state for this request: signed handoff first, then the cookie.

    ``?u=`` is only honoured when it is a valid signed token, so a visitor cannot
    self-upgrade by editing the URL.
    """
    handoff = verify_handoff(request.GET.get("u", ""))
    if handoff.is_premium:
        return handoff
    return read_cookie(request)


def attach_cookie(response, state: PremiumState) -> None:
    """Persist a verified premium state for the download session."""
    if not state.is_premium:
        return
    try:
        value = issue_cookie_value(state.subject)
    except PremiumError:
        return
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=cookie_ttl(),
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path="/",
    )


def record_assertion(subject: str, is_premium: bool) -> None:
    """
    Remember the blog's answer briefly (not a membership database).

    Expiry is enforced by every reader, and the row is replaced on change.
    """
    ttl = timedelta(seconds=cookie_ttl())
    PremiumAssertion.objects.update_or_create(
        subject_id=subject[:150],
        defaults={
            "is_premium": is_premium,
            "expires_at": timezone.now() + ttl,
            "source": "blog",
        },
    )


def cached_is_premium(subject: str) -> bool | None:
    """Cached assertion, or ``None`` when there is no current answer."""
    assertion = PremiumAssertion.objects.filter(subject_id=subject[:150]).first()
    if assertion is None or not assertion.is_current():
        return None
    return True

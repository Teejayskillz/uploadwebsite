"""
Download authorisation and byte delivery (brief sections 10 and 11).

How a download actually happens
-------------------------------
1. The visitor opens ``/d/<public_id>/`` — a normal page, no file bytes.
2. Pressing the button creates a :class:`DownloadSession` and a **signed,
   expiring token**; the browser is redirected to ``/dl/<token>/``.
3. ``/dl/`` verifies the token *and* the session's own expiry, then streams the
   file with Range support.
4. An expired or tampered token redirects back to the download page, which is
   the only place a fresh token can be minted (brief section 10).

The token is signed with Django's signing framework, so it cannot be forged,
and it contains only opaque ids — no paths, no filesystem information.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils import timezone

from uploader.models import DownloadEvent, DownloadSession, SiteSetting, Video

TOKEN_SALT = "nzdworld.download.token.v1"
CHUNK_SIZE = 64 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class DeliveryError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class TokenInvalid(DeliveryError):
    def __init__(self, message: str = "This download link is not valid."):
        super().__init__("invalid_token", message)


class TokenExpired(DeliveryError):
    def __init__(self, message: str = "This download link has expired."):
        super().__init__("expired_token", message)


@dataclass(frozen=True)
class TokenPayload:
    session_id: str
    video_id: str


# ---------------------------------------------------------------------------
# Sessions and tokens
# ---------------------------------------------------------------------------
def token_ttl_seconds() -> int:
    minutes = int(SiteSetting.load().download_token_ttl_minutes or 30)
    return max(1, minutes) * 60


def visitor_hash(request) -> str:
    """Salted, non-reversible visitor fingerprint used only for analytics."""
    raw = f"{request.META.get('REMOTE_ADDR', '')}|{request.META.get('HTTP_USER_AGENT', '')}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:64]


def create_session(
    video: Video, *, is_premium: bool, subject: str = "", request=None
) -> DownloadSession:
    """Open a download session. Expiry is the token TTL, not a fixed constant."""
    ttl = token_ttl_seconds()
    session = DownloadSession.objects.create(
        video=video,
        is_premium=is_premium,
        premium_subject=(subject or "")[:150],
        visitor_hash=visitor_hash(request) if request is not None else "",
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:300] if request else ""),
        referrer=(request.META.get("HTTP_REFERER", "")[:500] if request else ""),
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    DownloadEvent.objects.create(
        session=session, video=video, kind=DownloadEvent.Kind.SESSION_STARTED
    )
    return session


def issue_token(session: DownloadSession) -> str:
    payload = {"s": session.public_id, "v": session.video.public_id}
    return signing.dumps(payload, salt=TOKEN_SALT, compress=True)


def peek_token(token: str) -> TokenPayload | None:
    """
    Decode a token *without* enforcing expiry.

    Used only to find the video so an expired link can redirect to its download
    page instead of showing a dead end. Never grants file access on its own.
    """
    try:
        data = signing.loads(token, salt=TOKEN_SALT)
    except (signing.BadSignature, signing.SignatureExpired, ValueError):
        return None
    if not isinstance(data, dict) or "s" not in data or "v" not in data:
        return None
    return TokenPayload(session_id=str(data["s"]), video_id=str(data["v"]))


def verify_token(token: str) -> DownloadSession:
    """Full verification: signature, age, stored expiry, session and video state."""
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=token_ttl_seconds())
    except signing.SignatureExpired as exc:
        raise TokenExpired() from exc
    except (signing.BadSignature, ValueError) as exc:
        raise TokenInvalid() from exc

    session_id = str(data.get("s", ""))
    video_id = str(data.get("v", ""))
    session = (
        DownloadSession.objects.select_related("video")
        .filter(public_id=session_id)
        .first()
    )
    if session is None or session.video.public_id != video_id:
        raise TokenInvalid()
    if session.expires_at <= timezone.now():
        raise TokenExpired()
    if session.status in (DownloadSession.Status.CANCELLED, DownloadSession.Status.FAILED):
        raise TokenInvalid("This download session is no longer active.")
    if not session.video.is_available:
        raise TokenInvalid("This video is no longer available.")
    return session


# ---------------------------------------------------------------------------
# File resolution and delivery
# ---------------------------------------------------------------------------
def resolve_video_path(video: Video) -> Path:
    """Resolve a video to an absolute path inside VIDEO_ROOT (never from input)."""
    root = Path(settings.VIDEO_ROOT).resolve()
    candidate = (root / video.relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DeliveryError("unsafe_path", "Stored path escapes the storage root.") from exc
    if not candidate.is_file():
        raise DeliveryError("missing_file", "The stored file is missing.")
    return candidate


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """
    Parse a single ``Range: bytes=...`` header.

    Returns inclusive ``(start, end)`` or ``None`` when the header is absent or
    unsatisfiable. Multi-range requests are answered with the whole file (200),
    which is legal and keeps the implementation small and correct.
    """
    if not header or size <= 0:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    start_raw, end_raw = match.group(1), match.group(2)
    if not start_raw and not end_raw:
        return None
    if not start_raw:  # suffix range: last N bytes
        length = int(end_raw)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        if start >= size:
            return None
        end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def content_disposition(filename: str) -> str:
    """
    Build a safe Content-Disposition value.

    Strips quotes/CR/LF (the header-injection vector seen in the old proxy) and
    adds an RFC 5987 form so non-ASCII names survive.
    """
    safe_ascii = re.sub(r'[\r\n"\\]', "", filename or "download").strip() or "download"
    ascii_fallback = safe_ascii.encode("ascii", "ignore").decode("ascii") or "download"
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def serve_video(
    request, session: DownloadSession, *, track: bool = True
) -> HttpResponse:
    """Stream the file with Range support, recording analytics as it goes."""
    video = session.video
    path = resolve_video_path(video)
    size = path.stat().st_size
    content_type = video.mime_type or mimetypes.guess_type(video.stored_filename)[0] or "application/octet-stream"

    total_sent = 0
    is_range = False
    status = 200
    start, end = 0, size - 1
    parsed = parse_range(request.headers.get("Range", ""), size)
    if parsed is not None:
        start, end = parsed
        is_range = True
        status = 206

    length = max(0, end - start + 1)

    # Optional web-server handoff. Only enabled when the operator has proven it
    # works on their server; the default is honest app delivery.
    mode = str(getattr(settings, "DELIVERY_MODE", "django")).lower()
    header_enabled = bool(getattr(settings, "LITESPEED_INTERNAL_REDIRECT", False))
    if mode == "litespeed" and header_enabled and not is_range:
        response = HttpResponse(status=200)
        response["X-LiteSpeed-Location"] = f"/{quote(video.relative_path, safe='/')}"
        response["Content-Disposition"] = content_disposition(video.stored_filename)
        response["Content-Type"] = content_type
        if track:
            _record_first_byte(session, is_range=False)
        return response

    def chunks():
        nonlocal total_sent
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                total_sent += len(chunk)
                yield chunk
        if track:
            _record_completion(session, total_sent, is_range)

    response = StreamingHttpResponse(chunks(), status=status, content_type=content_type)
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition(video.stored_filename)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response


def _record_first_byte(session: DownloadSession, *, is_range: bool) -> None:
    DownloadSession.objects.filter(pk=session.pk).update(
        range_requests=session.range_requests + (1 if is_range else 0)
    )


def _record_completion(session: DownloadSession, sent: int, is_range: bool) -> None:
    """
    Persist delivery statistics.

    A Range continuation is *not* a new download: it increments
    ``range_requests`` and bytes, and only a non-Range full delivery marks the
    session completed (brief section 14).
    """
    DownloadSession.objects.filter(pk=session.pk).update(
        bytes_served=session.bytes_served + sent,
        range_requests=session.range_requests + (1 if is_range else 0),
    )
    event_kind = DownloadEvent.Kind.BYTES_SERVED
    DownloadEvent.objects.create(
        session=session,
        video=session.video,
        kind=event_kind,
        bytes_count=sent,
        is_range_request=is_range,
    )
    if not is_range:
        DownloadSession.objects.filter(pk=session.pk).update(
            status=DownloadSession.Status.COMPLETED, completed_at=timezone.now()
        )
        DownloadEvent.objects.create(
            session=session, video=session.video, kind=DownloadEvent.Kind.COMPLETED
        )


def record_failure(session: DownloadSession, detail: str) -> None:
    DownloadSession.objects.filter(pk=session.pk).update(status=DownloadSession.Status.FAILED)
    DownloadEvent.objects.create(
        session=session,
        video=session.video,
        kind=DownloadEvent.Kind.FAILED,
        detail=detail[:300],
    )


def record_expired(video: Video, detail: str = "") -> None:
    DownloadEvent.objects.create(
        video=video, kind=DownloadEvent.Kind.TOKEN_EXPIRED, detail=detail[:300]
    )


def record_invalid(video: Video | None, detail: str = "") -> None:
    DownloadEvent.objects.create(
        video=video, kind=DownloadEvent.Kind.TOKEN_INVALID, detail=detail[:300]
    )

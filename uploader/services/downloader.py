"""
Streaming HTTP downloader for the importer (brief sections 4 and 7).

Two properties matter more than speed here:

1. **SSRF cannot be reintroduced by the network layer.** The connection is made
   to an IP that :mod:`uploader.services.netguard` already approved, while the
   original hostname is still used for the ``Host`` header and TLS SNI. DNS is
   therefore never re-resolved between validation and transfer, which closes the
   rebinding window. Every redirect hop is validated again from scratch.
2. **Nothing is trusted about the payload.** The declared ``Content-Type`` is
   only a hint; the first bytes are sniffed against real container signatures, so
   an HTML error page served as ``video/mp4`` is rejected and deleted.

The transfer is streamed to a temporary file with a hard byte cap, an optional
cancel callback and a progress callback, and the SHA-256 is computed while
writing so duplicate detection later costs nothing extra.
"""

from __future__ import annotations

import hashlib
import http.client
import shutil
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .netguard import UnsafeURLError, ValidatedURL, validate_redirect

CHUNK_SIZE = 64 * 1024
SNIFF_BYTES = 512
MAX_REDIRECTS = 5
USER_AGENT = "NZDWorldImporter/1.0 (+https://nzdworld.com)"

HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml", b"<head", b"<body", b"<script")

CONTAINER_EXTENSIONS = {
    "mp4": "mp4",
    "mov": "mov",
    "m4v": "m4v",
    "mkv": "mkv",
    "webm": "webm",
    "avi": "avi",
    "flv": "flv",
    "ogg": "ogg",
    "mpeg": "mpg",
    "ts": "ts",
}


class DownloadError(Exception):
    """A failed transfer. ``code`` is stable enough for the admin UI to branch on."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = True):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    sha256: str
    container: str
    content_type: str
    final_url: str
    hops: int

    @property
    def extension(self) -> str:
        return CONTAINER_EXTENSIONS.get(self.container, self.container)


class Response(Protocol):
    """Minimal response surface, so tests can inject a fake transport."""

    status: int
    headers: dict[str, str]

    def read(self, amount: int) -> bytes: ...  # pragma: no cover - protocol

    def close(self) -> None: ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# Content sniffing
# ---------------------------------------------------------------------------
def sniff_container(head: bytes) -> str | None:
    """
    Identify a video container from its leading bytes.

    Returns a short container name, ``"html"`` for a web page masquerading as a
    video, or ``None`` when nothing matches.
    """
    if not head:
        return None

    stripped = head[:SNIFF_BYTES].lstrip().lower()
    if stripped.startswith(HTML_PREFIXES):
        return "html"

    # ISO base media (mp4 / mov / m4v): "....ftyp<brand>"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ", b"moov"):
            return "mov"
        if brand in (b"M4V ", b"M4VH", b"M4VP"):
            return "m4v"
        return "mp4"

    # Matroska / WebM share the EBML signature; the DocType disambiguates them.
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm" if b"webm" in head[:256].lower() else "mkv"

    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "avi"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"FLV\x01"):
        return "flv"
    if head.startswith(b"\x00\x00\x01\xba") or head.startswith(b"\x00\x00\x01\xb3"):
        return "mpeg"
    # MPEG-TS: 0x47 every 188 bytes.
    if head[0:1] == b"\x47" and (len(head) < 189 or head[188:189] == b"\x47"):
        return "ts"
    return None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class _SocketResponse:
    """Thin adapter over ``http.client`` that exposes only what we need."""

    def __init__(self, connection: http.client.HTTPConnection, response):
        self._connection = connection
        self._response = response
        self.status = int(response.status)
        self.headers = {key.lower(): value for key, value in response.getheaders()}

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        for closer in (self._response.close, self._connection.close):
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass


def open_http(
    validated: ValidatedURL,
    *,
    method: str = "GET",
    connect_timeout: float = 15.0,
    read_timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """
    Open a request against the validated IP, keeping the real host for Host/SNI.

    ``method`` defaults to GET (the importer); the URL checker uses HEAD.
    """
    ip = validated.addresses[0]
    try:
        raw_socket = socket.create_connection((ip, validated.port), timeout=connect_timeout)
    except OSError as exc:
        raise DownloadError("connect_failed", f"Could not connect: {exc}") from exc

    if validated.is_https:
        context = ssl.create_default_context()
        try:
            raw_socket = context.wrap_socket(raw_socket, server_hostname=validated.host)
        except ssl.SSLError as exc:
            raw_socket.close()
            raise DownloadError("tls_failed", f"TLS handshake failed: {exc}", retryable=False) from exc

    parts = urlsplit(validated.url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"

    connection = http.client.HTTPConnection(
        validated.host, validated.port, timeout=read_timeout
    )
    connection.sock = raw_socket  # bypasses DNS: we already chose the IP
    host_header = validated.host
    if validated.port not in (80, 443):
        host_header = f"{validated.host}:{validated.port}"

    try:
        connection.putrequest(method.upper(), target, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host_header)
        connection.putheader("User-Agent", USER_AGENT)
        connection.putheader("Accept", "video/*,application/octet-stream;q=0.9,*/*;q=0.5")
        connection.putheader("Accept-Encoding", "identity")
        connection.putheader("Connection", "close")
        for name, value in (extra_headers or {}).items():
            if name.lower() in {"host", "connection", "accept-encoding"}:
                continue
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        try:
            raw_socket.close()
        except OSError:
            pass
        raise DownloadError("request_failed", f"Request failed: {exc}") from exc

    return _SocketResponse(connection, response)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def fetch_to_file(
    validated: ValidatedURL,
    destination: Path,
    *,
    max_bytes: int,
    connect_timeout: float = 15.0,
    read_timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    on_progress: Callable[[int, int | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    transport: Callable[[ValidatedURL], Response] | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> DownloadResult:
    """
    Download to ``destination`` and prove it is a video.

    The destination is removed on every failure path, so a crashed import never
    leaves a half-file behind. ``transport`` exists for tests; production always
    uses :func:`open_http`.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = transport or (
        lambda target: open_http(
            target,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            extra_headers=extra_headers,
        )
    )

    current = validated
    hops = 0
    try:
        while True:
            response = opener(current)
            try:
                status = response.status
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if hops >= max_redirects:
                        raise DownloadError("too_many_redirects", "Redirect limit exceeded.")
                    if not location:
                        raise DownloadError("bad_redirect", "Redirect without a Location header.")
                    hops += 1
                    try:
                        current = validate_redirect(current.url, location)
                    except UnsafeURLError as exc:
                        raise DownloadError(
                            "unsafe_redirect", f"Redirect blocked: {exc}", retryable=False
                        ) from exc
                    continue

                if status != 200:
                    raise DownloadError(
                        f"http_{status}",
                        f"The source returned HTTP {status}.",
                        retryable=status >= 500 or status == 429,
                    )

                return _stream_to_file(
                    response,
                    destination=destination,
                    max_bytes=max_bytes,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                    final_url=current.url,
                    hops=hops,
                )
            finally:
                response.close()
    except DownloadError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, http.client.HTTPException) as exc:
        destination.unlink(missing_ok=True)
        raise DownloadError("transfer_failed", f"Transfer failed: {exc}") from exc


def _stream_to_file(
    response: Response,
    *,
    destination: Path,
    max_bytes: int,
    on_progress: Callable[[int, int | None], None] | None,
    should_cancel: Callable[[], bool] | None,
    final_url: str,
    hops: int,
) -> DownloadResult:
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in {"text/html", "application/xhtml+xml"}:
        raise DownloadError(
            "html_response", "The source returned a web page, not a video.", retryable=False
        )

    declared = _content_length(response.headers)
    if declared is not None and declared > max_bytes:
        raise DownloadError(
            "file_too_large",
            f"Declared size {declared} bytes exceeds the {max_bytes} byte limit.",
            retryable=False,
        )

    digest = hashlib.sha256()
    total = 0
    head = b""
    container: str | None = None
    last_reported = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("wb") as handle:
        while True:
            if should_cancel is not None and should_cancel():
                raise DownloadError("cancelled", "Cancelled by the administrator.", retryable=False)
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise DownloadError(
                    "file_too_large",
                    f"File exceeded the {max_bytes} byte limit during transfer.",
                    retryable=False,
                )
            if container is None and len(head) < SNIFF_BYTES:
                head += chunk[: SNIFF_BYTES - len(head)]
            if container is None and len(head) >= SNIFF_BYTES:
                container = _require_video(head)
            digest.update(chunk)
            handle.write(chunk)
            # Report at most every 1 MiB to keep the database quiet.
            if on_progress and total - last_reported >= 1024 * 1024:
                last_reported = total
                on_progress(total, declared)

    if total == 0:
        raise DownloadError("empty_response", "The source returned an empty body.", retryable=False)

    if container is None:
        container = _require_video(head)

    if on_progress:
        on_progress(total, declared or total)

    return DownloadResult(
        path=destination,
        size_bytes=total,
        sha256=digest.hexdigest(),
        container=container,
        content_type=content_type,
        final_url=final_url,
        hops=hops,
    )


def _require_video(head: bytes) -> str:
    container = sniff_container(head)
    if container == "html":
        raise DownloadError(
            "html_response", "The source returned a web page, not a video.", retryable=False
        )
    if container is None:
        raise DownloadError(
            "unknown_container",
            "The downloaded data does not match any supported video container.",
            retryable=False,
        )
    return container


def ensure_directory(path: Path) -> None:
    """Create a storage directory, failing loudly rather than silently."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError("storage_unavailable", f"Cannot create {path}: {exc}", retryable=False) from exc


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0

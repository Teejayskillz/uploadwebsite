"""
Lightweight URL verification for the admin UI.

Deliberately cheap: a HEAD request, falling back to a 2 KB ranged GET when the
host refuses HEAD (many file hosts do). It never downloads the file, and it never
replaces the importer's own validation -- the importer still re-checks everything
when the job actually runs.

Results are one of ``valid`` / ``invalid`` / ``inconclusive``. A plain HTTP 200 is
*not* enough to be ``valid``: the response must look like a file, not a web page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .downloader import DownloadError, open_http, sniff_container
from .netguard import UnsafeURLError, validate_redirect, validate_url

HEAD_UNSUPPORTED_STATUSES = {400, 401, 403, 405, 406, 501}
HTML_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}


@dataclass(frozen=True)
class UrlCheck:
    status: str  # valid | invalid | inconclusive
    reason: str
    http_status: int | None = None
    content_type: str = ""
    content_length: int | None = None
    method: str = ""
    final_url: str = ""
    filename: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _invalid(reason, **kwargs) -> UrlCheck:
    return UrlCheck(status="invalid", reason=reason, **kwargs)


def _inconclusive(reason, **kwargs) -> UrlCheck:
    return UrlCheck(status="inconclusive", reason=reason, **kwargs)


def _valid(reason, **kwargs) -> UrlCheck:
    return UrlCheck(status="valid", reason=reason, **kwargs)


def _open(validated, method: str, timeout: float | None = None, extra_headers=None):
    return open_http(
        validated,
        method=method,
        connect_timeout=timeout or 6.0,
        read_timeout=timeout or 10.0,
        extra_headers=extra_headers,
    )


def _content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _filename_from_url(url: str) -> str:
    from urllib.parse import unquote, urlsplit

    return unquote((urlsplit(url).path or "").rsplit("/", 1)[-1])


def verify_url(url: str, *, opener=None, max_redirects: int = 3) -> UrlCheck:
    """
    Check accessibility and whether the target plausibly is a file.

    ``opener`` is injectable for tests: ``opener(validated, method) -> response``.
    """
    open_fn = opener or _open
    try:
        current = validate_url(url)
    except UnsafeURLError as exc:
        return _invalid(f"URL rejected: {exc}")

    hops = 0
    while True:
        try:
            response = open_fn(current, "HEAD")
        except DownloadError as exc:
            return _inconclusive(f"Could not reach the host: {exc.message}")

        try:
            status = response.status
            headers = dict(response.headers)
        finally:
            response.close()

        if status in (301, 302, 303, 307, 308):
            location = headers.get("location", "")
            if hops >= max_redirects:
                return _invalid("Too many redirects.", http_status=status, method="HEAD")
            if not location:
                return _invalid("Redirect without a Location header.", http_status=status, method="HEAD")
            hops += 1
            try:
                current = validate_redirect(current.url, location)
            except UnsafeURLError as exc:
                return _invalid(f"Redirect blocked: {exc}", http_status=status, method="HEAD")
            continue

        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        length = _content_length(headers)
        filename = _filename_from_url(current.url)
        base = dict(
            http_status=status,
            content_type=content_type,
            content_length=length,
            final_url=current.url,
            filename=filename,
        )

        if status in HEAD_UNSUPPORTED_STATUSES:
            return _probe_get(open_fn, current, base, hops)
        if status >= 400:
            return _invalid(f"The server returned HTTP {status}.", method="HEAD", **base)
        if content_type in HTML_TYPES:
            return _invalid("That URL returns a web page, not a file.", method="HEAD", **base)
        if length == 0:
            return _inconclusive("The server reports an empty file.", method="HEAD", **base)

        if content_type.startswith("video/") or content_type in {
            "application/octet-stream",
            "application/mp4",
            "application/x-matroska",
            "audio/mpeg",
            "application/zip",
        }:
            return _valid(
                f"Looks like a downloadable file ({content_type or 'unknown type'}).",
                method="HEAD",
                **base,
            )
        return _inconclusive(
            "The server responded, but did not identify the content type.",
            method="HEAD",
            **base,
        )


def _probe_get(open_fn, validated, base: dict, hops: int) -> UrlCheck:
    """HEAD was refused, so read the first couple of KB instead of guessing."""
    headers = {"Range": "bytes=0-2047"}
    try:
        response = open_fn(validated, "GET", extra_headers=headers)
    except TypeError:
        # Opener without extra-headers support (tests may provide a simple one).
        try:
            response = open_fn(validated, "GET")
        except DownloadError as exc:
            return _inconclusive(f"Could not verify: {exc.message}", method="GET", **base)
    except DownloadError as exc:
        return _inconclusive(f"Could not verify: {exc.message}", method="GET", **base)

    try:
        status = response.status
        headers_in = dict(response.headers)
        chunk = response.read(2048) if status in (200, 206) else b""
    finally:
        response.close()

    merged = dict(base)
    merged["http_status"] = status
    merged["content_type"] = (headers_in.get("content-type") or base.get("content_type") or "").split(";")[0].strip().lower()
    merged["content_length"] = _content_length(headers_in) or base.get("content_length")

    if status in (301, 302, 303, 307, 308):
        return _inconclusive("The URL redirects; try again or check the final link.", method="GET", **merged)
    if status >= 400:
        return _invalid(f"The server returned HTTP {status}.", method="GET", **merged)

    container = sniff_container(chunk)
    if container == "html":
        return _invalid("That URL returns a web page, not a file.", method="GET", **merged)
    if container is not None:
        return _valid(f"Looks like a {container.upper()} file.", method="GET", **merged)
    return _inconclusive(
        "The server responded, but the first bytes are not a recognised file format.",
        method="GET",
        **merged,
    )

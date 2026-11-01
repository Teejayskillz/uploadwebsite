"""
SSRF protection for the URL importer (brief section 4).

Every URL that the importer is about to fetch — the first request *and* every
redirect hop — must pass :func:`validate_url`. The function resolves the host
and rejects the URL if **any** resolved address is private, loopback,
link-local, multicast, reserved or unspecified. That "any" rule is deliberate:
a hostile DNS record can return one public and one internal address, and
``requests`` may pick either.

The validated address list is returned so the downloader can connect to a
verified IP while still sending the original ``Host`` header / TLS SNI, which
closes the DNS-rebinding window between validation and connection.

This module has no Django imports and no global state, so it is unit-testable
with an injected resolver and no network access.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# Hostnames that must never be contacted regardless of what DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

# Suffixes reserved for local/special-use networks.
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)


class UnsafeURLError(ValueError):
    """Raised when a URL must not be fetched. Message is safe to log."""


@dataclass(frozen=True)
class ValidatedURL:
    """A URL that passed validation, plus the IPs it may be reached on."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 is loopback even though the v6 object says otherwise.
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (hasattr(ip, "is_site_local") and ip.is_site_local)
    )


def check_address(address: str) -> str:
    """Validate one literal IP address string; returns it canonicalised."""
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError as exc:
        raise UnsafeURLError(f"Not an IP address: {address!r}") from exc
    if not _is_public_ip(ip):
        raise UnsafeURLError(f"Address is not publicly routable: {ip}")
    return str(ip)


def _resolve(
    host: str, port: int, resolver
) -> tuple[str, ...]:
    """
    Resolve ``host`` and return its distinct IP strings.

    ``resolver`` mirrors ``socket.getaddrinfo``'s signature so tests can inject
    a fake. Any resolution failure is a hard error: an importer that "tries
    again later" on DNS failure is an SSRF bypass waiting to happen.
    """
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        raise UnsafeURLError(f"Could not resolve host {host!r}: {exc}") from exc

    addresses: list[str] = []
    for info in infos or ():
        try:
            sockaddr = info[4]
            candidate = sockaddr[0]
        except (IndexError, TypeError):
            continue
        if candidate and candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise UnsafeURLError(f"Host {host!r} did not resolve to any address.")
    return tuple(addresses)


def validate_url(
    url: str,
    *,
    resolver=socket.getaddrinfo,
    allow_ports: tuple[int, ...] = (80, 443),
    allow_http: bool = True,
) -> ValidatedURL:
    """
    Validate an outbound URL and return its pinned addresses.

    Raises :class:`UnsafeURLError` for anything the importer must not fetch.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("Empty URL.")
    raw = url.strip()

    # Reject control characters outright: they enable header/CRLF trickery and
    # are never legitimate in a video URL.
    if any(ch in raw for ch in ("\r", "\n", "\t", "\x00")):
        raise UnsafeURLError("URL contains control characters.")

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Scheme {scheme or '(none)'!r} is not allowed; use http or https.")
    if scheme == "http" and not allow_http:
        raise UnsafeURLError("Plain http is disabled; use https.")

    if parts.username or parts.password:
        raise UnsafeURLError("URLs containing credentials are not allowed.")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host.")

    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid port in URL: {exc}") from exc
    if port not in allow_ports:
        raise UnsafeURLError(f"Port {port} is not allowed.")

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(f"Host {host!r} is blocked.")

    # IP literals are checked directly; hostnames are resolved and every answer
    # must be public.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        addresses = (check_address(str(literal)),)
    else:
        addresses = tuple(check_address(addr) for addr in _resolve(host, port, resolver))

    # Rebuild the URL without credentials/fragment so the downloader can never
    # re-introduce either.
    normalised = urlunsplit(
        (scheme, parts.netloc.split("@")[-1], parts.path or "/", parts.query, "")
    )
    return ValidatedURL(
        url=normalised, scheme=scheme, host=host, port=port, addresses=addresses
    )


def validate_redirect(base_url: str, location: str, **kwargs) -> ValidatedURL:
    """
    Validate a redirect target relative to the URL that produced it.

    Redirects are followed manually by the downloader precisely so that each hop
    goes through here; nothing may inherit the trust of the previous hop.
    """
    from urllib.parse import urljoin

    if not location:
        raise UnsafeURLError("Empty redirect target.")
    target = urljoin(base_url, location.strip())
    return validate_url(target, **kwargs)


def safe_host_label(validated: ValidatedURL) -> str:
    """Log-safe description of a validated URL (never includes the query string)."""
    return f"{validated.scheme}://{validated.host}:{validated.port}{urlsplit(validated.url).path}"

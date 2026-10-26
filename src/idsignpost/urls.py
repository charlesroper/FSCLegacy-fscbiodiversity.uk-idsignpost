"""URL sanitization, normalization, and public-address validation."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

BASE_URL = "https://www.fscbiodiversity.uk/idsignpost/"

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "srsltid",
    }
)

_PATH_SAFE = "/%~:@!$&'()*+,;=-_"
_QUERY_SAFE = "/?%~:@!$&'()*+,;=-_"
_MARKDOWN_LINK = re.compile(r"^\[.*?\]\(<([^>]+)>\)$|^\[.*?\]\(([^)]+)\)$")


def extract_plain_url(value: str) -> str:
    """Return the URL from Markdown link syntax or the stripped input."""
    stripped = value.strip()
    match = _MARKDOWN_LINK.match(stripped)
    if match:
        return (match.group(1) or match.group(2)).strip()
    return stripped


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMS


def _encode_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(_is_tracking_param(name) for name, _ in pairs):
        # Keep the legacy page's own encoding (for example %2C or +) so stored
        # URLs stay stable across extractions.
        return quote(query, safe=_QUERY_SAFE + "%")
    kept = [(name, value) for name, value in pairs if not _is_tracking_param(name)]
    return "&".join(
        f"{quote(name, safe='')}={quote(value, safe=_QUERY_SAFE)}"
        for name, value in kept
    )


def sanitize_url(raw_url: str, base: str = BASE_URL) -> str:
    """Resolve, strip tracking parameters, and RFC 3986 encode a URL."""
    url = urljoin(base, extract_plain_url(raw_url))
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            quote(parts.path, safe=_PATH_SAFE),
            _encode_query(parts.query),
            quote(parts.fragment, safe=_QUERY_SAFE),
        )
    )


def strip_fragment(url: str) -> str:
    """Remove the fragment from an already sanitized URL."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def cache_key(url: str) -> str:
    """Return the URL-tier or entry-tier key base with the fragment removed."""
    return strip_fragment(sanitize_url(url))


def clean_host(url: str) -> str:
    """Return a lowercase host with any leading www. removed."""
    host = (urlsplit(extract_plain_url(url)).hostname or "").lower()
    return host.removeprefix("www.")


def _remove_single_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/") and not path.endswith("//"):
        return path[:-1]
    return path


def normalized_url(url: str) -> str:
    """Normalize a URL for equality checks across redirects."""
    parts = urlsplit(strip_fragment(extract_plain_url(url)))
    host = clean_host(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port not in {80, 443}:
        host = f"{host}:{port}"
    path = _remove_single_trailing_slash(parts.path)
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def urls_equal(left: str, right: str) -> bool:
    """Compare two URLs after normalization."""
    return normalized_url(left) == normalized_url(right)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _socket_addresses(resolver: Callable[..., Iterable[Any]], host: str) -> list[str]:
    try:
        results = resolver(host, None)
    except OSError:
        return []
    addresses: list[str] = []
    for result in results:
        sockaddr = result[4]
        if sockaddr:
            addresses.append(str(sockaddr[0]))
    return addresses


def is_public_http_url(
    url: str,
    *,
    resolver: Callable[..., Iterable[Any]] = socket.getaddrinfo,
) -> bool:
    """Return True when a URL uses HTTP or HTTPS and resolves only to public IPs."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            return False
        if not parts.hostname:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        parts.port
    except ValueError:
        return False

    literal = _parse_ip(parts.hostname)
    if literal is not None:
        return _is_public_ip(literal)

    addresses = _socket_addresses(resolver, parts.hostname)
    if not addresses:
        return False

    parsed = [_parse_ip(address) for address in addresses]
    if any(address is None for address in parsed):
        return False
    return all(_is_public_ip(address) for address in parsed if address is not None)

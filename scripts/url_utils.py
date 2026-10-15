#!/usr/bin/env python3
"""URL normalization and extraction utilities."""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

BASE_URL = "https://www.fscbiodiversity.uk/idsignpost/"


def extract_plain_url(value: str) -> str:
    """Extract a plain URL from markdown link syntax like [text](<url>) or [text](url)."""
    stripped = value.strip()
    match = re.match(r"^\[.*?\]\(<([^>]+)>\)$", stripped) or re.match(
        r"^\[.*?\]\(([^)]+)\)$", stripped
    )
    if match:
        return match.group(1).strip()
    return stripped


def sanitize_url(raw_url: str) -> str:
    """Normalize and ensure a URL is a valid RFC 3986 URI."""
    url = extract_plain_url(raw_url)

    # Resolve relative URLs against the legacy FSC base URL
    if not url.startswith(("http://", "https://", "ftp://", "mailto:")):
        url = urljoin(BASE_URL, url)

    parts = urlsplit(url)
    path = quote(parts.path, safe="/%~:@!$&'()*+,;=-_")
    query = quote(parts.query, safe="%~:@!$&'()*+,;=/?-_")
    fragment = quote(parts.fragment, safe="%~:@!$&'()*+,;=/?-_")

    return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))

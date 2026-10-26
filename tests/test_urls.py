"""Tests for URL sanitization and validation."""

from __future__ import annotations

import pytest

from idsignpost.urls import (
    BASE_URL,
    cache_key,
    clean_host,
    extract_plain_url,
    is_public_http_url,
    sanitize_url,
    urls_equal,
)

PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str, port: object = None) -> list[tuple[object, ...]]:
    return [(2, 1, 6, "", (PUBLIC_IP, 0))]


def private_resolver(host: str, port: object = None) -> list[tuple[object, ...]]:
    return [(2, 1, 6, "", ("10.0.0.7", 0))]


def test_extract_plain_url_handles_markdown_and_plain_text() -> None:
    assert (
        extract_plain_url("[guide](<https://example.org/a b>)")
        == "https://example.org/a b"
    )
    assert (
        extract_plain_url("[guide](https://example.org/a)") == "https://example.org/a"
    )
    assert extract_plain_url("  https://example.org/a  ") == "https://example.org/a"


def test_sanitize_resolves_relative_links_against_legacy_site() -> None:
    assert sanitize_url("guides/bats.pdf") == f"{BASE_URL}guides/bats.pdf"
    assert (
        sanitize_url("../../about.html") == "https://www.fscbiodiversity.uk/about.html"
    )
    assert sanitize_url("/downloads/bats.pdf") == (
        "https://www.fscbiodiversity.uk/downloads/bats.pdf"
    )


def test_sanitize_percent_encodes_spaces() -> None:
    assert sanitize_url("https://example.org/my guide.pdf") == (
        "https://example.org/my%20guide.pdf"
    )


def test_sanitize_strips_tracking_parameters_and_keeps_the_rest() -> None:
    url = (
        "https://example.org/guide.pdf?utm_source=news&utm_medium=email"
        "&fbclid=abc&gclid=def&msclkid=1&mc_cid=2&mc_eid=3&ref=home"
        "&srsltid=xyz&id=42"
    )

    assert sanitize_url(url) == "https://example.org/guide.pdf?id=42"


def test_sanitize_keeps_meaningful_query_order() -> None:
    assert sanitize_url("https://example.org/a?b=2&a=1") == (
        "https://example.org/a?b=2&a=1"
    )


def test_sanitize_preserves_existing_query_encoding() -> None:
    url = "https://example.org/a?dm_i=IG4%2C3L55N&q=a+b"

    assert sanitize_url(url) == url


def test_fragment_is_kept_in_stored_url_but_dropped_from_cache_key() -> None:
    url = "https://example.org/guide.pdf?utm_source=x#page-4"

    assert sanitize_url(url) == "https://example.org/guide.pdf#page-4"
    assert cache_key(url) == "https://example.org/guide.pdf"


def test_clean_host_lowercases_and_removes_www() -> None:
    assert clean_host("HTTPS://WWW.Example.ORG/a") == "example.org"
    assert clean_host("https://sub.example.org/a") == "sub.example.org"


def test_urls_equal_ignores_single_trailing_slash_and_fragment() -> None:
    assert urls_equal("https://www.example.org/guide/", "https://example.org/guide#top")
    assert not urls_equal("https://example.org/guide", "https://example.org/other")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file",
        "mailto:someone@example.org",
        "https://user:secret@example.org/",
        "https://127.0.0.1/",
        "https://10.0.0.1/",
        "https://192.168.1.1/",
        "https://169.254.1.1/",
        "https://[::1]/",
        "https://[fe80::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://0.0.0.0/",
    ],
)
def test_public_url_rejects_unsafe_targets(url: str) -> None:
    assert not is_public_http_url(url, resolver=public_resolver)


def test_public_url_accepts_public_literal_and_resolved_names() -> None:
    assert is_public_http_url(f"https://{PUBLIC_IP}/guide", resolver=private_resolver)
    assert is_public_http_url("https://example.org/guide", resolver=public_resolver)


def test_public_url_rejects_private_dns_results() -> None:
    assert not is_public_http_url(
        "https://example.org/guide", resolver=private_resolver
    )


def test_public_url_rejects_dns_failure() -> None:
    def failing_resolver(host: str, port: object = None) -> list[tuple[object, ...]]:
        raise OSError("no such host")

    assert not is_public_http_url("https://missing.example/", resolver=failing_resolver)

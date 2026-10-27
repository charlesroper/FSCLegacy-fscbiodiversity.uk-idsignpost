"""Tests for the two-tier verification cache."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from idsignpost.cache import (
    CACHE_VERSION,
    SAVE_EVERY,
    CatalogCache,
    EntryRecord,
    UrlRecord,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
URL = "https://example.org/guide.pdf"


def clock() -> datetime:
    return NOW


def make_cache(tmp_path: Path, **overrides: Any) -> CatalogCache:
    return CatalogCache.load(tmp_path / ".url_cache.json", now=clock, **overrides)


def read_payload(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / ".url_cache.json").read_text(encoding="utf-8"))


def test_round_trip_url_and_entry_records(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(
        f"{URL}#page-4",
        UrlRecord(
            status="404",
            state="broken",
            resolved_url="https://example.org/missing",
            page_title="Not found",
            page_h1="Missing",
        ),
    )
    cache.put_entry(
        "a-bats",
        URL,
        EntryRecord(
            redirect_is_match=False,
            alternative_url="https://example.org/new",
            alternative_probability=0.9,
            alternative_reasoning="Moved.",
        ),
    )
    cache.save()

    payload = read_payload(tmp_path)
    assert payload["schema_version"] == CACHE_VERSION
    assert list(payload["urls"]) == [URL]
    assert list(payload["entries"]) == [f"a-bats::{URL}"]

    reloaded = make_cache(tmp_path)
    url_record = reloaded.get_url(URL)
    assert url_record is not None
    assert url_record.state == "broken"
    assert url_record.status == "404"
    assert url_record.page_title == "Not found"
    assert url_record.checked_at == NOW

    entry_record = reloaded.get_entry("a-bats", URL)
    assert entry_record is not None
    assert entry_record.redirect_is_match is False
    assert entry_record.alternative_probability == pytest.approx(0.9)


def test_fragment_is_ignored_in_both_tiers(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(f"{URL}#a", UrlRecord(status="200", state="ok"))
    cache.put_entry("a-bats", f"{URL}#b", EntryRecord(redirect_is_match=True))

    assert cache.get_url(URL) is not None
    assert cache.get_entry("a-bats", f"{URL}#c") is not None
    assert len(cache.urls) == 1
    assert len(cache.entries) == 1


def test_shared_url_keeps_entries_isolated(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(URL, UrlRecord(status="404", state="broken"))
    cache.put_entry("first", URL, EntryRecord(alternative_url="https://one.example"))
    cache.put_entry("second", URL, EntryRecord(alternative_url="https://two.example"))
    cache.save()

    reloaded = make_cache(tmp_path)
    assert reloaded.get_url(URL).status == "404"  # type: ignore[union-attr]
    assert reloaded.get_entry("first", URL).alternative_url == "https://one.example"  # type: ignore[union-attr]
    assert reloaded.get_entry("second", URL).alternative_url == "https://two.example"  # type: ignore[union-attr]


def test_expired_records_are_not_returned(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(
        URL, UrlRecord(status="200", state="ok", checked_at=NOW - timedelta(days=31))
    )
    cache.put_entry(
        "a-bats",
        URL,
        EntryRecord(
            alternative_url="https://old.example", checked_at=NOW - timedelta(days=31)
        ),
    )

    assert cache.get_url(URL) is None
    assert cache.get_entry("a-bats", URL) is None


def test_fresh_records_within_ttl_are_returned(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(
        URL, UrlRecord(status="200", state="ok", checked_at=NOW - timedelta(days=29))
    )

    assert cache.get_url(URL) is not None


def test_save_drops_expired_records(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(
        URL, UrlRecord(status="200", state="ok", checked_at=NOW - timedelta(days=31))
    )
    cache.save()

    assert read_payload(tmp_path)["urls"] == {}


def test_transient_results_are_not_stored(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)

    assert (
        cache.put_url(URL, UrlRecord(status="timeout", state="broken"), cacheable=False)
        is False
    )
    assert (
        cache.put_entry(
            "a-bats", URL, EntryRecord(redirect_is_match=False), cacheable=False
        )
        is False
    )
    assert len(cache) == 0


def test_transient_results_do_not_overwrite_good_records(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(URL, UrlRecord(status="200", state="ok"))

    cache.put_url(URL, UrlRecord(status="503", state="broken"), cacheable=False)

    record = cache.get_url(URL)
    assert record is not None
    assert record.status == "200"
    assert record.state == "ok"


def test_negative_verdicts_are_cached(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_entry(
        "a-bats",
        URL,
        EntryRecord(redirect_is_match=False, redirect_reason="Generic homepage."),
    )
    cache.save()

    record = make_cache(tmp_path).get_entry("a-bats", URL)
    assert record is not None
    assert record.redirect_is_match is False
    assert record.alternative_url is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps(
            {"schema_version": 1, "urls": {URL: {"status": "200"}}, "entries": {}}
        ),
        json.dumps(["not", "a", "mapping"]),
    ],
)
def test_mismatched_or_malformed_cache_is_discarded(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / ".url_cache.json"
    path.write_text(content, encoding="utf-8")

    cache = CatalogCache.load(path, now=clock)

    assert len(cache) == 0


def test_invalid_records_are_skipped_on_load(tmp_path: Path) -> None:
    path = tmp_path / ".url_cache.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_VERSION,
                "urls": {
                    URL: {"status": "200", "state": "alive"},
                    "https://example.org/ok": {
                        "status": "200",
                        "state": "ok",
                        "checked_at": NOW.isoformat(),
                    },
                    "https://example.org/undated": {"status": "200", "state": "ok"},
                },
                "entries": {},
            }
        ),
        encoding="utf-8",
    )

    cache = CatalogCache.load(path, now=clock)

    assert cache.get_url(URL) is None
    assert cache.get_url("https://example.org/undated") is None
    assert cache.get_url("https://example.org/ok") is not None


def test_saves_every_twenty_five_writes(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)

    for index in range(SAVE_EVERY):
        cache.put_url(
            f"https://example.org/{index}", UrlRecord(status="200", state="ok")
        )
        assert (tmp_path / ".url_cache.json").exists() == (index == SAVE_EVERY - 1)

    assert cache.pending_writes == 0
    assert len(read_payload(tmp_path)["urls"]) == SAVE_EVERY


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(URL, UrlRecord(status="200", state="ok"))
    cache.save()

    assert sorted(path.name for path in tmp_path.iterdir()) == [".url_cache.json"]


def test_get_and_set_wayback_on_new_and_existing_url(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    snapshot = "https://web.archive.org/web/2026/https://example.org/new"

    assert cache.get_wayback("https://example.org/new") is None
    cache.set_wayback("https://example.org/new", snapshot)
    assert cache.get_wayback("https://example.org/new") == snapshot

    cache.put_url("https://example.org/existing", UrlRecord(status="200", state="ok"))
    cache.set_wayback(
        "https://example.org/existing",
        "https://web.archive.org/web/2026/https://example.org/existing",
    )
    assert (
        cache.get_wayback("https://example.org/existing")
        == "https://web.archive.org/web/2026/https://example.org/existing"
    )

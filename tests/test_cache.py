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
    normalize_title,
)
from idsignpost.http_checker import CheckResult

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
        "Bats in flight",
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
    assert list(payload["entries"]) == [f"bats in flight::{URL}"]

    reloaded = make_cache(tmp_path)
    url_record = reloaded.get_url(URL)
    assert url_record is not None
    assert url_record.state == "broken"
    assert url_record.status == "404"
    assert url_record.page_title == "Not found"
    assert url_record.checked_at == NOW

    entry_record = reloaded.get_entry("Bats in flight", URL)
    assert entry_record is not None
    assert entry_record.redirect_is_match is False
    assert entry_record.alternative_probability == pytest.approx(0.9)


def test_fragment_is_ignored_in_both_tiers(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(f"{URL}#a", UrlRecord(status="200", state="ok"))
    cache.put_entry("Bats in flight", f"{URL}#b", EntryRecord(redirect_is_match=True))

    assert cache.get_url(URL) is not None
    assert cache.get_entry("Bats in flight", f"{URL}#c") is not None
    assert len(cache.urls) == 1
    assert len(cache.entries) == 1


def test_shared_url_keeps_entries_isolated(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(URL, UrlRecord(status="404", state="broken"))
    cache.put_entry(
        "First guide", URL, EntryRecord(alternative_url="https://one.example")
    )
    cache.put_entry(
        "Second guide", URL, EntryRecord(alternative_url="https://two.example")
    )
    cache.save()

    reloaded = make_cache(tmp_path)
    url_record = reloaded.get_url(URL)
    first_record = reloaded.get_entry("First guide", URL)
    second_record = reloaded.get_entry("Second guide", URL)
    assert url_record is not None
    assert first_record is not None
    assert second_record is not None
    assert url_record.status == "404"
    assert first_record.alternative_url == "https://one.example"
    assert second_record.alternative_url == "https://two.example"


def test_expired_records_are_not_returned(tmp_path: Path) -> None:
    cache = make_cache(tmp_path)
    cache.put_url(
        URL, UrlRecord(status="200", state="ok", checked_at=NOW - timedelta(days=31))
    )
    cache.put_entry(
        "Bats in flight",
        URL,
        EntryRecord(
            alternative_url="https://old.example", checked_at=NOW - timedelta(days=31)
        ),
    )

    assert cache.get_url(URL) is None
    assert cache.get_entry("Bats in flight", URL) is None


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
            "Bats in flight",
            URL,
            EntryRecord(redirect_is_match=False),
            cacheable=False,
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
        "Bats in flight",
        URL,
        EntryRecord(redirect_is_match=False, redirect_reason="Generic homepage."),
    )
    cache.save()

    record = make_cache(tmp_path).get_entry("Bats in flight", URL)
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


def test_check_result_round_trip_and_ttl_expiry(tmp_path: Path) -> None:
    current = NOW
    cache = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    result = CheckResult(
        status="200",
        state="ok",
        resolved_url="https://example.org/final",
        is_suspicious_redirect=True,
        page_title="Guide Title",
        page_h1="Main Heading",
        page_snippet="Intro snippet",
    )

    assert cache.get_check(URL) is None
    assert cache.put_check(f"{URL}#page-2", result) is True
    cache.save()

    payload = read_payload(tmp_path)
    assert payload["schema_version"] == CACHE_VERSION
    assert list(payload["urls"]) == [URL]

    reloaded = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    assert reloaded.get_check(URL) == result
    assert reloaded.get_check(f"{URL}#other") == result

    current = NOW + timedelta(days=31)
    assert reloaded.get_check(URL) is None


def test_put_check_preserves_wayback_link_on_refresh(tmp_path: Path) -> None:
    current = NOW
    cache = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    snapshot = "https://web.archive.org/web/2026/https://example.org/guide.pdf"
    wayback_only = "https://example.org/wayback-first"
    other_snapshot = (
        "https://web.archive.org/web/2026/https://example.org/wayback-first"
    )

    cache.set_wayback(wayback_only, other_snapshot)
    assert cache.get_check(wayback_only) is None
    assert cache.put_check(wayback_only, CheckResult("200", "ok")) is True
    assert cache.get_wayback(wayback_only) == other_snapshot

    cache.put_check(URL, CheckResult("200", "ok"))
    cache.set_wayback(URL, snapshot)
    assert cache.put_check(URL, CheckResult("404", "broken")) is True
    cache.save()

    reloaded = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    assert reloaded.get_check(URL) == CheckResult("404", "broken")
    assert reloaded.get_wayback(URL) == snapshot

    current = NOW + timedelta(days=31)
    assert reloaded.put_check(URL, CheckResult("200", "ok")) is True
    assert reloaded.get_wayback(URL) is None


def test_put_check_ignores_transient_failure_without_overwriting(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)
    snapshot = "https://web.archive.org/web/2026/https://example.org/guide.pdf"
    cache.put_check(URL, CheckResult("200", "ok"))
    cache.set_wayback(URL, snapshot)
    writes_before = cache.pending_writes

    assert cache.put_check(URL, CheckResult("503", "blocked", cacheable=False)) is False
    assert cache.pending_writes == writes_before
    assert cache.get_check(URL) == CheckResult("200", "ok")
    assert cache.get_wayback(URL) == snapshot

    assert (
        cache.put_check(
            "https://example.org/new",
            CheckResult("timeout", "blocked", cacheable=False),
        )
        is False
    )
    assert cache.get_check("https://example.org/new") is None


def test_drop_alternative_clears_alternative_and_keeps_redirect_verdict(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)
    cache.put_entry(
        "Bats in flight",
        URL,
        EntryRecord(
            redirect_is_match=False,
            redirect_reason="Generic homepage",
            alternative_url="https://hub.example/list",
            alternative_probability=0.8,
            alternative_reasoning="Shared hub.",
            alternative_note="confirmed: matches subject",
            alternative_checked=True,
            alternative_confirmed=True,
        ),
    )
    note = "dropped: URL shared by three or more entries"

    assert cache.drop_alternative("Bats in flight", f"{URL}#frag", note) is True
    cache.save()

    reloaded = make_cache(tmp_path)
    record = reloaded.get_entry("Bats in flight", URL)
    assert record is not None
    assert record.redirect_is_match is False
    assert record.redirect_reason == "Generic homepage"
    assert record.alternative_url is None
    assert record.alternative_probability is None
    assert record.alternative_reasoning is None
    assert record.alternative_note == note
    assert record.alternative_checked is True
    assert record.alternative_confirmed is False


def test_drop_alternative_returns_false_when_absent_or_expired(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)

    assert cache.drop_alternative("Missing guide", URL, "dropped: missing") is False
    assert cache.pending_writes == 0

    cache.put_entry(
        "Expired guide",
        URL,
        EntryRecord(
            alternative_url="https://old.example/guide",
            checked_at=NOW - timedelta(days=31),
        ),
    )
    writes_before = cache.pending_writes
    assert cache.drop_alternative("Expired guide", URL, "dropped: expired") is False
    assert cache.pending_writes == writes_before


def test_update_entry_stores_new_record_without_carrying_stale_redirect(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)

    created = cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Generic homepage.",
    )
    assert created.redirect_is_match is False
    assert created.redirect_reason == "Generic homepage."
    assert created.checked_at == NOW

    updated = cache.update_entry(
        "Bats in flight",
        f"{URL}#frag",
        alternative_url="https://alt.example/guide",
        alternative_probability=0.92,
        alternative_reasoning="Direct guide.",
        alternative_note="confirmed: official guide",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    assert updated.redirect_is_match is None
    assert updated.redirect_reason is None
    assert updated.alternative_url == "https://alt.example/guide"

    cache.save()
    payload = read_payload(tmp_path)
    assert payload["schema_version"] == CACHE_VERSION
    assert list(payload["entries"]) == [f"bats in flight::{URL}"]

    reloaded = make_cache(tmp_path).get_entry("Bats in flight", URL)
    assert reloaded is not None
    assert reloaded.redirect_is_match is None
    assert reloaded.redirect_reason is None
    assert reloaded.alternative_url == "https://alt.example/guide"
    assert reloaded.alternative_probability == pytest.approx(0.92)
    assert reloaded.alternative_reasoning == "Direct guide."
    assert reloaded.alternative_note == "confirmed: official guide"
    assert reloaded.alternative_checked is True
    assert reloaded.alternative_confirmed is True


def test_record_redirect_preserves_alternative_and_skips_unchanged(
    tmp_path: Path,
) -> None:
    t0 = NOW - timedelta(days=2)
    current = t0
    cache = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    cache.update_entry(
        "Bats in flight",
        URL,
        alternative_url="https://alt.example/guide",
        alternative_probability=0.85,
        alternative_reasoning="Moved.",
        alternative_note="confirmed: matches guide",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    cache.save()
    assert cache.pending_writes == 0

    current = NOW
    recorded = cache.record_redirect(
        "Bats in flight", f"{URL}#frag", is_match=False, reason="Generic homepage."
    )
    assert recorded is not None
    assert cache.pending_writes == 1
    assert recorded.redirect_is_match is False
    assert recorded.redirect_reason == "Generic homepage."
    assert recorded.alternative_url == "https://alt.example/guide"
    assert recorded.alternative_confirmed is True
    assert recorded.checked_at == NOW

    cache.save()
    current = NOW + timedelta(hours=1)
    unchanged = cache.record_redirect(
        "Bats in flight", URL, is_match=False, reason="Generic homepage."
    )
    assert unchanged is not None
    assert cache.pending_writes == 0
    assert unchanged.checked_at == NOW

    cleared = cache.record_redirect("Bats in flight", URL, is_match=None)
    assert cleared is not None
    assert cleared.redirect_is_match is None
    assert cleared.redirect_reason is None
    assert cleared.alternative_url == "https://alt.example/guide"
    assert cache.pending_writes == 1


def test_clear_entry_removes_stale_redirect_and_alternative_state(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)
    assert cache.clear_entry("Missing guide", URL) is False
    assert cache.pending_writes == 0

    cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Generic homepage.",
        alternative_note="no candidates returned",
        alternative_checked=True,
    )
    cache.save()

    assert cache.clear_entry("Bats in flight", f"{URL}#frag") is True
    assert cache.pending_writes == 1
    assert cache.get_entry("Bats in flight", URL) is None
    cache.save()

    assert read_payload(tmp_path)["entries"] == {}


def test_update_entry_replaces_redirect_verdict_and_clears_alternative(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)
    cache.put_entry(
        "Bats in flight",
        URL,
        EntryRecord(
            redirect_is_match=False,
            redirect_reason="Generic homepage.",
            alternative_url="https://alt.example/guide",
            alternative_probability=0.9,
            alternative_reasoning="Direct guide.",
            alternative_note="confirmed: official guide",
            alternative_checked=True,
            alternative_confirmed=True,
        ),
    )

    updated = cache.update_entry("Bats in flight", URL, redirect_is_match=True)

    assert updated.redirect_is_match is True
    assert updated.redirect_reason is None
    assert updated.alternative_url is None
    assert updated.alternative_checked is False
    assert updated.alternative_confirmed is False


def test_transient_entry_result_does_not_overwrite_good_entry_record(
    tmp_path: Path,
) -> None:
    cache = make_cache(tmp_path)
    cache.update_entry(
        "Bats in flight",
        URL,
        alternative_url="https://alt.example/guide",
        alternative_probability=0.9,
        alternative_note="confirmed: official guide",
        alternative_checked=True,
        alternative_confirmed=True,
    )

    assert (
        cache.put_entry(
            "Bats in flight", URL, EntryRecord(redirect_is_match=True), cacheable=False
        )
        is False
    )
    record = cache.get_entry("Bats in flight", URL)
    assert record is not None
    assert record.redirect_is_match is None
    assert record.alternative_url == "https://alt.example/guide"
    assert record.alternative_confirmed is True


def test_update_entry_skips_write_when_semantic_fields_are_unchanged(
    tmp_path: Path,
) -> None:
    t0 = NOW - timedelta(days=5)
    current = t0
    cache = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Generic homepage.",
        alternative_url="https://alt.example/guide",
        alternative_probability=0.85,
        alternative_reasoning="Moved.",
        alternative_note="confirmed: matches guide",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    cache.save()
    assert cache.pending_writes == 0

    current = NOW
    unchanged_explicit = cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Generic homepage.",
        alternative_url="https://alt.example/guide",
        alternative_probability=0.85,
        alternative_reasoning="Moved.",
        alternative_note="confirmed: matches guide",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    assert cache.pending_writes == 0
    assert unchanged_explicit.checked_at == t0

    changed = cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Generic homepage.",
        alternative_url="https://alt.example/guide",
        alternative_probability=0.85,
        alternative_reasoning="Moved.",
        alternative_note="confirmed: updated note",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    assert cache.pending_writes == 1
    assert changed.checked_at == NOW


def test_update_entry_writes_when_absent_or_expired(tmp_path: Path) -> None:
    current = NOW - timedelta(days=31)
    cache = CatalogCache.load(tmp_path / ".url_cache.json", now=lambda: current)
    cache.update_entry(
        "Bats in flight",
        URL,
        redirect_is_match=False,
        redirect_reason="Old mismatch.",
    )
    cache.save()

    current = NOW
    refreshed = cache.update_entry("Bats in flight", URL)
    assert cache.pending_writes == 1
    assert refreshed.checked_at == NOW
    assert refreshed.redirect_is_match is None
    assert refreshed.redirect_reason is None


def test_normalize_title_and_entry_key_matching(tmp_path: Path) -> None:
    assert normalize_title("  Bats   in\t\nFLIGHT  ") == "bats in flight"

    cache = make_cache(tmp_path)
    cache.put_entry(
        "  Bats   in\tFlight ",
        f"{URL}#frag-1",
        EntryRecord(
            alternative_url="https://alt.example/bats",
            alternative_checked=True,
            alternative_confirmed=True,
        ),
    )
    cache.save()

    payload = read_payload(tmp_path)
    assert list(payload["entries"]) == [f"bats in flight::{URL}"]

    reloaded = make_cache(tmp_path)
    hit = reloaded.get_entry("bats in flight", f"{URL}#frag-2")
    assert hit is not None
    assert hit.alternative_url == "https://alt.example/bats"


def test_legacy_entry_key_migrates_on_read_and_saves_under_normalized_title(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".url_cache.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_VERSION,
                "urls": {},
                "entries": {
                    f"legacy-bats::{URL}": {
                        "redirect_is_match": False,
                        "redirect_reason": "Generic homepage.",
                        "alternative_url": "https://alt.example/bats",
                        "alternative_probability": 0.9,
                        "alternative_reasoning": "Moved.",
                        "alternative_note": "confirmed: official guide",
                        "alternative_checked": True,
                        "alternative_confirmed": True,
                        "checked_at": NOW.isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cache = make_cache(tmp_path)
    assert cache.get_entry("Bats in flight", URL) is None

    migrated = cache.get_entry("Bats in flight", URL, entry_id="legacy-bats")
    assert migrated is not None
    assert migrated.alternative_url == "https://alt.example/bats"
    assert cache.pending_writes == 1

    cache.save()
    payload = read_payload(tmp_path)
    assert list(payload["entries"]) == [f"bats in flight::{URL}"]

    reloaded = make_cache(tmp_path)
    assert reloaded.get_entry("Bats in flight", URL) is not None


def test_expired_legacy_entry_key_is_not_returned_or_migrated(tmp_path: Path) -> None:
    path = tmp_path / ".url_cache.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_VERSION,
                "urls": {},
                "entries": {
                    f"legacy-bats::{URL}": {
                        "alternative_url": "https://old.example/bats",
                        "alternative_checked": True,
                        "checked_at": (NOW - timedelta(days=31)).isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cache = make_cache(tmp_path)
    assert cache.get_entry("Bats in flight", URL, entry_id="legacy-bats") is None
    assert cache.pending_writes == 0
    assert f"bats in flight::{URL}" not in cache.entries


def test_put_and_clear_remove_legacy_entry_key(tmp_path: Path) -> None:
    path = tmp_path / ".url_cache.json"
    legacy_payload = {
        "schema_version": CACHE_VERSION,
        "urls": {},
        "entries": {
            f"legacy-bats::{URL}": {
                "alternative_url": "https://old.example/bats",
                "alternative_checked": True,
                "checked_at": NOW.isoformat(),
            }
        },
    }
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    cache = make_cache(tmp_path)
    cache.put_entry(
        "Bats in flight",
        URL,
        EntryRecord(alternative_url="https://new.example/bats"),
        entry_id="legacy-bats",
    )
    assert f"legacy-bats::{URL}" not in cache.entries
    assert f"bats in flight::{URL}" in cache.entries

    path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    cache2 = make_cache(tmp_path)
    assert cache2.clear_entry("Bats in flight", URL, entry_id="legacy-bats") is True
    assert cache2.entries == {}

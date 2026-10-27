"""Tests for the verification pipeline state machine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from idsignpost.cache import CatalogCache, EntryRecord
from idsignpost.gemini_finder import Alternative, DiscoveryOutcome, RedirectVerdict
from idsignpost.http_checker import CheckResult
from idsignpost.models import CatalogEntry
from idsignpost.pipeline import (
    OfflineRunError,
    Verifier,
    merge_verified,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
URL = "https://example.org/guide"


def entry(entry_id: str = "guide", **overrides: object) -> CatalogEntry:
    data: dict[str, object] = {
        "id": entry_id,
        "filename": f"{entry_id}.html",
        "title": "Bats in flight",
        "web_link": URL,
    }
    data.update(overrides)
    return CatalogEntry.model_validate(data)


class FakeChecker:
    def __init__(self, results: dict[str, CheckResult], *, abort: bool = False) -> None:
        self.results = results
        self.checked: list[str] = []
        self.abort = abort

    async def check(self, url: str, *, entry_title: str | None = None) -> CheckResult:
        self.checked.append(url)
        return self.results[url]

    def should_abort(self) -> bool:
        return self.abort


class FakeFinder:
    def __init__(
        self,
        *,
        redirects: list[RedirectVerdict | None] | None = None,
        discoveries: dict[str, DiscoveryOutcome] | None = None,
    ) -> None:
        self.redirects = list(redirects or [])
        self.discoveries = discoveries or {}
        self.redirect_calls: list[dict[str, object]] = []
        self.discovery_calls: list[str] = []
        self.preferred_calls: list[Alternative | None] = []
        self.confirm_calls: list[tuple[str, str]] = []
        self.confirmations: dict[str, bool] = {}

    async def verify_redirect(self, **kwargs: object) -> RedirectVerdict | None:
        self.redirect_calls.append(kwargs)
        return self.redirects.pop(0) if self.redirects else None

    async def find_alternative(
        self,
        entry: CatalogEntry,
        *,
        checker: object,
        preferred: Alternative | None = None,
    ) -> DiscoveryOutcome:
        self.discovery_calls.append(entry.id)
        self.preferred_calls.append(preferred)
        return self.discoveries.get(entry.id, DiscoveryOutcome())

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
    ) -> RedirectVerdict | None:
        self.confirm_calls.append((entry.id, url))
        return RedirectVerdict(self.confirmations.get(entry.id, True), "model note")


class FakeArchive:
    def __init__(
        self, snapshots: dict[str, str] | None = None, save_result: str | None = None
    ) -> None:
        self.snapshots = snapshots or {}
        self.save_result = save_result
        self.lookups: list[str] = []
        self.saves: list[str] = []

    async def lookup(self, url: str) -> str | None:
        self.lookups.append(url)
        return self.snapshots.get(url)

    async def save(self, url: str) -> str | None:
        self.saves.append(url)
        return self.save_result


def make_verifier(
    tmp_path: object,
    checker: FakeChecker,
    *,
    finder: FakeFinder | None = None,
    archive: FakeArchive | None = None,
    cache: CatalogCache | None = None,
    save_archive: bool = False,
) -> Verifier:
    if cache is None:
        cache = CatalogCache(
            path=tmp_path / "cache.json",  # type: ignore[operator]
            ttl_days=30,
            now=lambda: NOW,
        )
    return Verifier(
        cache=cache,
        checker=checker,
        finder=finder or FakeFinder(),
        wayback=archive or FakeArchive(),
        save_archive=save_archive,
    )


def live(resolved: str | None = None, *, suspicious: bool = False) -> CheckResult:
    return CheckResult(
        "200",
        "ok",
        resolved_url=resolved,
        is_suspicious_redirect=suspicious,
        page_title="Bats in flight",
    )


async def test_live_link_is_ok_and_gets_a_snapshot(tmp_path: object) -> None:
    checker = FakeChecker({URL: live()})
    archive = FakeArchive(
        {URL: "https://web.archive.org/web/2020/https://example.org/x"}
    )
    verifier = make_verifier(tmp_path, checker, archive=archive)

    result = await verifier.verify([entry()])

    updated = result[0]
    assert updated.web_link_state == "ok"
    assert updated.web_link_status == "200"
    assert updated.wayback_link == archive.snapshots[URL]
    assert updated.alternative_link is None
    assert updated.verified_at == NOW.isoformat()
    assert verifier.stats.checked == 1
    assert verifier.stats.states == {"ok": 1}


async def test_suspicious_redirect_confirmed_by_gemini_stays_ok(
    tmp_path: object,
) -> None:
    checker = FakeChecker({URL: live("https://other.example/guide", suspicious=True)})
    finder = FakeFinder(redirects=[RedirectVerdict(True, "Same guide.")])
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "ok"
    assert updated.web_link_resolved == "https://other.example/guide"
    assert finder.redirect_calls[0]["page_title"] == "Bats in flight"


async def test_redirect_mismatch_becomes_broken_with_alternative(
    tmp_path: object,
) -> None:
    checker = FakeChecker({URL: live("https://other.example/", suspicious=True)})
    finder = FakeFinder(
        redirects=[RedirectVerdict(False, "Generic homepage.")],
        discoveries={
            "guide": DiscoveryOutcome(
                Alternative("https://new.example/guide", 0.8, "Moved here.")
            )
        },
    )
    archive = FakeArchive()
    verifier = make_verifier(
        tmp_path, checker, finder=finder, archive=archive, save_archive=True
    )

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "broken"
    assert updated.web_link_status == "redirect_mismatch"
    assert updated.alternative_link == "https://new.example/guide"
    assert updated.alternative_probability == 0.8
    assert updated.alternative_reasoning == "Moved here."
    assert archive.saves == []
    assert verifier.stats.alternatives == 1


async def test_broken_link_discovers_a_replacement(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder(
        discoveries={
            "guide": DiscoveryOutcome(Alternative("https://new.example/guide", 0.7))
        }
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.web_link_state == "broken"
    assert updated.web_link_status == "404"
    assert updated.alternative_link == "https://new.example/guide"
    assert finder.redirect_calls == []


async def test_blocked_link_gets_no_alternative(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("403", "blocked")})
    finder = FakeFinder()
    archive = FakeArchive()
    verifier = make_verifier(
        tmp_path, checker, finder=finder, archive=archive, save_archive=True
    )

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "blocked"
    assert updated.alternative_link is None
    assert finder.discovery_calls == []
    assert archive.lookups == [URL]
    assert archive.saves == []


async def test_shared_url_is_checked_once(tmp_path: object) -> None:
    checker = FakeChecker({URL: live()})
    archive = FakeArchive()
    verifier = make_verifier(tmp_path, checker, archive=archive)
    entries = [entry("a"), entry("b")]

    result = await verifier.verify(entries)

    assert checker.checked == [URL]
    assert archive.lookups == [URL]
    assert [item.web_link_state for item in result] == ["ok", "ok"]


async def test_transient_check_preserves_good_fields(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("503", "blocked", cacheable=False)})
    cached = entry(
        web_link_state="ok",
        web_link_status="200",
        web_link_resolved="https://example.org/final",
        wayback_link="https://web.archive.org/web/2020/x",
        verified_at="2026-01-01T00:00:00+00:00",
    )

    updated = (await make_verifier(tmp_path, checker).verify([cached]))[0]

    assert updated.model_dump() == cached.model_dump()


async def test_transient_check_on_a_fresh_entry_is_recorded_but_not_cached(
    tmp_path: object,
) -> None:
    checker = FakeChecker({URL: CheckResult("timeout", "blocked", cacheable=False)})
    verifier = make_verifier(tmp_path, checker)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "blocked"
    assert updated.web_link_status == "timeout"
    assert updated.verified_at is None
    assert len(verifier.cache.urls) == 0


async def test_second_run_reuses_the_url_cache(tmp_path: object) -> None:
    checker = FakeChecker({URL: live()})
    archive = FakeArchive({URL: "https://web.archive.org/web/2020/x"})
    first = make_verifier(tmp_path, checker, archive=archive)
    await first.verify([entry()])
    first.cache.save()

    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        archive=FakeArchive(),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]

    assert second.stats.checked == 0
    assert second.stats.cache_hits == 1
    assert updated.web_link_state == "ok"
    assert updated.wayback_link == "https://web.archive.org/web/2020/x"
    assert updated.verified_at is None


async def test_negative_discovery_is_cached(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder()
    first = make_verifier(tmp_path, checker, finder=finder)
    await first.verify([entry()])
    first.cache.save()

    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=FakeFinder(),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]

    assert len(finder.discovery_calls) == 1
    assert second.stats.checked == 0
    assert updated.web_link_state == "broken"
    assert updated.alternative_link is None


async def test_transient_gemini_failure_keeps_previous_state(tmp_path: object) -> None:
    checker = FakeChecker({URL: live("https://other.example/", suspicious=True)})
    finder = FakeFinder(redirects=[None])
    cached = entry(
        web_link_state="ok",
        web_link_status="200",
        verified_at="2026-01-01T00:00:00+00:00",
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([cached]))[
        0
    ]

    assert updated.model_dump() == cached.model_dump()


async def test_alternative_is_cleared_when_the_link_recovers(tmp_path: object) -> None:
    stale = entry(
        alternative_link="https://old.example/guide",
        alternative_probability=0.9,
        alternative_reasoning="Old.",
    )

    updated = (
        await make_verifier(tmp_path, FakeChecker({URL: live()})).verify([stale])
    )[0]

    assert updated.web_link_state == "ok"
    assert updated.alternative_link is None
    assert updated.alternative_probability is None
    assert updated.alternative_reasoning is None


async def test_offline_guard_aborts_without_writing_the_cache(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("connect_error", "broken")}, abort=True)
    verifier = make_verifier(tmp_path, checker)

    with pytest.raises(OfflineRunError):
        await verifier.verify([entry()])

    assert not (tmp_path / "cache.json").exists()  # type: ignore[operator]


def test_merge_updates_only_verified_entries() -> None:
    catalog = [entry("a"), entry("b"), entry("c")]
    verified = [entry("b", web_link_state="ok")]

    merged = merge_verified(catalog, verified)

    assert [item.web_link_state for item in merged] == [None, "ok", None]


def test_merge_is_fatal_for_unknown_ids() -> None:
    with pytest.raises(ValueError, match="not in catalog"):
        merge_verified([entry("a")], [entry("missing")])


def test_merge_is_fatal_for_duplicate_verified_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        merge_verified([entry("a")], [entry("a"), entry("a")])


def test_merge_clears_verification_when_the_link_changed() -> None:
    catalog = [entry("a", web_link="https://example.org/new")]
    verified = [
        entry(
            "a",
            web_link="https://example.org/old",
            web_link_state="ok",
            web_link_status="200",
            verified_at="2026-01-01T00:00:00+00:00",
        )
    ]

    merged = merge_verified(catalog, verified)

    assert merged[0].web_link_state is None
    assert merged[0].web_link_status is None
    assert merged[0].verified_at is None


async def test_alternative_shared_by_three_entries_is_dropped(tmp_path: object) -> None:
    entries = [
        entry(f"guide-{index}", web_link=f"https://example.org/{index}")
        for index in range(3)
    ]
    checker = FakeChecker(
        {item.web_link or "": CheckResult("404", "broken") for item in entries}
    )
    shared = Alternative("https://hub.example/list", 0.8)
    finder = FakeFinder(
        discoveries={item.id: DiscoveryOutcome(shared) for item in entries}
    )
    verifier = make_verifier(tmp_path, checker, finder=finder)

    result = await verifier.verify(entries)

    assert all(item.alternative_link is None for item in result)
    record = verifier.cache.get_entry("guide-0", entries[0].web_link or "")
    assert record is not None
    assert record.alternative_url is None
    assert record.alternative_checked is True


async def test_alternative_shared_by_two_entries_is_kept(tmp_path: object) -> None:
    entries = [
        entry(f"guide-{index}", web_link=f"https://example.org/{index}")
        for index in range(2)
    ]
    checker = FakeChecker(
        {item.web_link or "": CheckResult("404", "broken") for item in entries}
    )
    shared = Alternative("https://publisher.example/guide", 0.9)
    finder = FakeFinder(
        discoveries={item.id: DiscoveryOutcome(shared) for item in entries}
    )

    result = await make_verifier(tmp_path, checker, finder=finder).verify(entries)

    assert [item.alternative_link for item in result] == [
        "https://publisher.example/guide",
        "https://publisher.example/guide",
    ]


async def test_cached_image_alternative_is_cleared(tmp_path: object) -> None:
    url = "https://example.org/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_entry(
        "guide",
        url,
        EntryRecord(
            alternative_url="https://example.org/scan.jpg",
            alternative_probability=0.8,
            alternative_checked=True,
        ),
    )
    checker = FakeChecker({url: CheckResult("404", "broken")})
    verifier = make_verifier(tmp_path, checker, cache=cache)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link is None
    assert updated.alternative_probability is None
    record = cache.get_entry("guide", url)
    assert record is not None
    assert record.alternative_url is None
    assert record.alternative_checked is True
    assert checker.checked == [url]


async def test_discovery_receives_the_previous_alternative(tmp_path: object) -> None:
    previous = Alternative("https://publisher.example/guide", 0.7, "older result")
    cached = entry(
        alternative_link=previous.url,
        alternative_probability=previous.probability,
        alternative_reasoning=previous.reasoning,
    )
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder()
    verifier = make_verifier(tmp_path, checker, finder=finder)

    await verifier.verify([cached])

    assert finder.preferred_calls == [previous]


async def test_cached_alternative_is_confirmed_before_reuse(tmp_path: object) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_entry(
        "guide",
        URL,
        EntryRecord(
            alternative_url=alt_url,
            alternative_probability=0.9,
            alternative_checked=True,
        ),
    )
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live(alt_url)})
    finder = FakeFinder()
    verifier = make_verifier(tmp_path, checker, finder=finder, cache=cache)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link == alt_url
    assert finder.confirm_calls == [("guide", alt_url)]
    record = cache.get_entry("guide", URL)
    assert record is not None
    assert record.alternative_confirmed is True


async def test_cached_alternative_is_dropped_when_confirmation_fails(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_entry(
        "guide",
        URL,
        EntryRecord(
            alternative_url=alt_url,
            alternative_probability=0.9,
            alternative_checked=True,
        ),
    )
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live(alt_url)})
    finder = FakeFinder()
    finder.confirmations["guide"] = False
    verifier = make_verifier(tmp_path, checker, finder=finder, cache=cache)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link is None
    record = cache.get_entry("guide", URL)
    assert record is not None
    assert record.alternative_url is None
    assert record.alternative_confirmed is False


async def test_confirmed_alternative_is_reused_without_another_call(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_entry(
        "guide",
        URL,
        EntryRecord(
            alternative_url=alt_url,
            alternative_probability=0.9,
            alternative_checked=True,
            alternative_confirmed=True,
        ),
    )
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder()
    verifier = make_verifier(tmp_path, checker, finder=finder, cache=cache)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link == alt_url
    assert finder.confirm_calls == []


async def test_discovery_note_is_stored_with_the_alternative(tmp_path: object) -> None:
    note = "confirmed: official product page for this handbook"
    finder = FakeFinder(
        discoveries={
            "guide": DiscoveryOutcome(
                Alternative("https://publisher.example/guide", 0.8),
                confirmed=True,
                note=note,
            )
        }
    )
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_note == note
    record = verifier.cache.get_entry("guide", URL)
    assert record is not None
    assert record.alternative_note == note


async def test_negative_search_note_is_stored(tmp_path: object) -> None:
    note = "rejected: category index"
    finder = FakeFinder(discoveries={"guide": DiscoveryOutcome(note=note)})
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link is None
    assert updated.alternative_note == note
    record = verifier.cache.get_entry("guide", URL)
    assert record is not None
    assert record.alternative_note == note

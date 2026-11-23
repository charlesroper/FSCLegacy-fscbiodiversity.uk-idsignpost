"""Tests for the verification pipeline state machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import asyncio
import json
from pathlib import Path
import socket
from typing import Any

import httpx
import pytest
import respx

from idsignpost.cache import CatalogCache
from idsignpost.gemini_finder import Candidate, GenerationResult, RedirectVerdict
from idsignpost.http_checker import CheckResult
from idsignpost.models import CatalogEntry
from idsignpost.pipeline import (
    OfflineRunError,
    Verifier,
    VerifyConfig,
    VerifyDeps,
    merge_verified,
    run_verification,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
URL = "https://example.org/guide"


def public_resolver(host: str, port: Any) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


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
        return self.results.get(url, CheckResult("404", "broken"))

    def should_abort(self) -> bool:
        return self.abort


class FakeFinder:
    def __init__(
        self,
        *,
        redirects: list[RedirectVerdict | None] | None = None,
        candidates: dict[str, list[Candidate] | None] | None = None,
        confirmations: dict[str, RedirectVerdict | None] | None = None,
    ) -> None:
        self.redirects = list(redirects or [])
        self.candidates = candidates or {}
        self.confirmations = confirmations or {}
        self.redirect_calls: list[dict[str, object]] = []
        self.discovery_calls: list[str] = []
        self.confirm_calls: list[tuple[str, str]] = []

    async def verify_redirect(self, **kwargs: object) -> RedirectVerdict | None:
        self.redirect_calls.append(kwargs)
        return self.redirects.pop(0) if self.redirects else None

    async def discover(
        self, entry: CatalogEntry, *, hint: str | None = None
    ) -> list[Candidate] | None:
        self.discovery_calls.append(entry.id)
        return self.candidates.get(entry.id, [])

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None = None,
    ) -> RedirectVerdict | None:
        self.confirm_calls.append((entry.id, url))
        if url in self.confirmations:
            return self.confirmations[url]
        if entry.id in self.confirmations:
            return self.confirmations[entry.id]
        return RedirectVerdict(True, None)


class FakeArchive:
    def __init__(
        self,
        snapshots: dict[str, str | None] | None = None,
        save_result: str | None = None,
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
    refresh: bool = False,
    concurrency: int = 10,
    gemini_concurrency: int = 3,
    resolver: Callable[..., Any] | None = public_resolver,
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
        concurrency=concurrency,
        gemini_concurrency=gemini_concurrency,
        save_archive=save_archive,
        refresh=refresh,
        resolver=resolver,
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
    alt_url = "https://new.example/guide"
    checker = FakeChecker(
        {
            URL: live("https://other.example/", suspicious=True),
            alt_url: live(alt_url),
        }
    )
    finder = FakeFinder(
        redirects=[RedirectVerdict(False, "Generic homepage.")],
        candidates={"guide": [Candidate(alt_url, 0.8, "Moved here.")]},
    )
    archive = FakeArchive()
    verifier = make_verifier(
        tmp_path, checker, finder=finder, archive=archive, save_archive=True
    )

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "broken"
    assert updated.web_link_status == "redirect_mismatch"
    assert updated.alternative_link == alt_url
    assert updated.alternative_probability == 0.8
    assert updated.alternative_reasoning == "Moved here."
    assert archive.saves == [alt_url]
    assert verifier.stats.alternatives == 1


async def test_broken_link_discovers_a_replacement(tmp_path: object) -> None:
    alt_url = "https://new.example/guide"
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live(alt_url)})
    finder = FakeFinder(candidates={"guide": [Candidate(alt_url, 0.7)]})

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.web_link_state == "broken"
    assert updated.web_link_status == "404"
    assert updated.alternative_link == alt_url
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
    verifier.cache.save()

    assert updated.web_link_state == "blocked"
    assert updated.web_link_status == "timeout"
    assert updated.verified_at is None

    second_checker = FakeChecker({URL: live()})
    second = make_verifier(
        tmp_path,
        second_checker,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]

    assert second_checker.checked == [URL]
    assert second.stats.cache_hits == 0
    assert rerun.web_link_state == "ok"


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

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]

    assert len(finder.discovery_calls) == 1
    assert second_finder.discovery_calls == []
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
    shared_url = "https://hub.example/list"
    results = {item.web_link or "": CheckResult("404", "broken") for item in entries}
    results[shared_url] = live(shared_url)
    checker = FakeChecker(results)
    finder = FakeFinder(
        candidates={item.id: [Candidate(shared_url, 0.8)] for item in entries}
    )
    verifier = make_verifier(tmp_path, checker, finder=finder)

    result = await verifier.verify(entries)
    verifier.cache.save()

    assert all(item.alternative_link is None for item in result)
    assert all(
        item.alternative_note == "dropped: URL shared by three or more entries"
        for item in result
    )

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = await second.verify(entries)

    assert all(item.alternative_link is None for item in rerun)
    assert second_finder.discovery_calls == []


async def test_alternative_shared_by_two_entries_is_kept(tmp_path: object) -> None:
    entries = [
        entry(f"guide-{index}", web_link=f"https://example.org/{index}")
        for index in range(2)
    ]
    shared_url = "https://publisher.example/guide"
    results = {item.web_link or "": CheckResult("404", "broken") for item in entries}
    results[shared_url] = live(shared_url)
    checker = FakeChecker(results)
    finder = FakeFinder(
        candidates={item.id: [Candidate(shared_url, 0.9)] for item in entries}
    )

    result = await make_verifier(tmp_path, checker, finder=finder).verify(entries)

    assert [item.alternative_link for item in result] == [shared_url, shared_url]


async def test_cached_image_alternative_is_cleared(tmp_path: object) -> None:
    url = "https://example.org/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.update_entry(
        "guide",
        url,
        alternative_url="https://example.org/scan.jpg",
        alternative_probability=0.8,
        alternative_checked=True,
    )
    checker = FakeChecker({url: CheckResult("404", "broken")})
    verifier = make_verifier(tmp_path, checker, cache=cache)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link is None
    assert updated.alternative_probability is None
    assert updated.alternative_note == "ignored: blocked host or image file"
    assert checker.checked == [url]


async def test_discovery_tries_previous_alternative_before_searching(
    tmp_path: object,
) -> None:
    previous_url = "https://publisher.example/guide"
    cached = entry(
        alternative_link=previous_url,
        alternative_probability=0.7,
        alternative_reasoning="older result",
    )
    checker = FakeChecker({URL: CheckResult("404", "broken"), previous_url: live()})
    finder = FakeFinder(
        confirmations={previous_url: RedirectVerdict(True, "still matches")}
    )
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([cached]))[0]

    assert updated.alternative_link == previous_url
    assert updated.alternative_probability == 0.7
    assert updated.alternative_reasoning == "older result"
    assert updated.alternative_note == "confirmed: still matches"
    assert finder.discovery_calls == []
    assert finder.confirm_calls == [("guide", previous_url)]


async def test_cached_alternative_is_confirmed_before_reuse(tmp_path: object) -> None:
    alt_url = "https://publisher.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: None},
        ),
    )
    unconfirmed = (await first.verify([entry()]))[0]
    first.cache.save()
    assert unconfirmed.alternative_link == alt_url

    second_finder = FakeFinder(
        confirmations={alt_url: RedirectVerdict(True, "confirmed on rerun")}
    )
    second = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    confirmed = (await second.verify([entry()]))[0]
    second.cache.save()

    assert confirmed.alternative_link == alt_url
    assert confirmed.alternative_note == "confirmed: confirmed on rerun"
    assert second_finder.discovery_calls == []
    assert second_finder.confirm_calls == [("guide", alt_url)]

    third_finder = FakeFinder()
    third = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=third_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    reused = (await third.verify([entry()]))[0]

    assert reused.alternative_link == alt_url
    assert third_finder.confirm_calls == []


async def test_cached_alternative_is_dropped_when_confirmation_fails(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: None},
        ),
    )
    await first.verify([entry()])
    first.cache.save()

    second = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            confirmations={alt_url: RedirectVerdict(False, "different book")}
        ),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]
    second.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == "rejected: different book"

    third_finder = FakeFinder()
    third = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=third_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await third.verify([entry()]))[0]

    assert rerun.alternative_link is None
    assert third_finder.confirm_calls == []
    assert third_finder.discovery_calls == []


async def test_confirmed_alternative_is_reused_without_another_call(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: RedirectVerdict(True, "official guide")},
        ),
    )
    await first.verify([entry()])
    first.cache.save()

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]

    assert updated.alternative_link == alt_url
    assert updated.alternative_note == "confirmed: official guide"
    assert second_finder.confirm_calls == []
    assert second_finder.discovery_calls == []


async def test_discovery_note_is_stored_with_the_alternative(tmp_path: object) -> None:
    alt_url = "https://publisher.example/guide"
    reasoning = "official product page for this handbook"
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.8)]},
        confirmations={alt_url: RedirectVerdict(True, reasoning)},
    )
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]
    verifier.cache.save()

    assert updated.alternative_note == f"confirmed: {reasoning}"

    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]
    assert rerun.alternative_note == f"confirmed: {reasoning}"


async def test_negative_search_note_is_stored(tmp_path: object) -> None:
    alt_url = "https://hub.example/list"
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.8)]},
        confirmations={alt_url: RedirectVerdict(False, "category index")},
    )
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]
    verifier.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == "rejected: category index"

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]

    assert rerun.alternative_link is None
    assert rerun.alternative_note == "rejected: category index"
    assert second_finder.discovery_calls == []


async def test_transient_discovery_preserves_existing_note(tmp_path: object) -> None:
    alt_url = "https://publisher.example/guide"
    existing = entry(
        web_link_state="broken",
        web_link_status="404",
        alternative_link=alt_url,
        alternative_probability=0.8,
        alternative_reasoning="Official guide.",
        alternative_note="confirmed: official product page",
        verified_at="2026-01-01T00:00:00+00:00",
    )
    finder = FakeFinder(candidates={"guide": None})
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            alt_url: CheckResult("503", "blocked", cacheable=False),
        }
    )
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([existing]))[0]
    verifier.cache.save()

    assert updated.alternative_link == alt_url
    assert updated.alternative_note == "confirmed: official product page"
    assert updated.verified_at == "2026-01-01T00:00:00+00:00"

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    await second.verify([existing])
    assert second_finder.confirm_calls == [("guide", alt_url)]


async def test_broken_link_with_alternative_falls_back_to_alternative_snapshot(
    tmp_path: object,
) -> None:
    alt_url = "https://new.example/guide"
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.9)]},
        confirmations={alt_url: RedirectVerdict(True, "matches")},
    )
    archive = FakeArchive(
        {
            URL: None,  # no original snapshot
            alt_url: "https://web.archive.org/web/2026/https://new.example/guide",
        }
    )
    verifier = make_verifier(tmp_path, checker, finder=finder, archive=archive)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.web_link_state == "broken"
    assert updated.alternative_link == alt_url
    assert (
        updated.wayback_link
        == "https://web.archive.org/web/2026/https://new.example/guide"
    )
    assert verifier.cache.get_wayback(alt_url) == updated.wayback_link
    assert verifier.cache.get_wayback(URL) is None


async def test_broken_link_with_original_snapshot_keeps_original_wayback(
    tmp_path: object,
) -> None:
    alt_url = "https://new.example/guide"
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.9)]},
        confirmations={alt_url: RedirectVerdict(True, "matches")},
    )
    archive = FakeArchive(
        {
            URL: "https://web.archive.org/web/2020/https://example.org/guide",
            alt_url: "https://web.archive.org/web/2026/https://new.example/guide",
        }
    )
    verifier = make_verifier(tmp_path, checker, finder=finder, archive=archive)

    updated = (await verifier.verify([entry()]))[0]

    assert (
        updated.wayback_link
        == "https://web.archive.org/web/2020/https://example.org/guide"
    )
    assert alt_url not in archive.lookups


async def test_save_archive_captures_unarchived_alternative_link(
    tmp_path: object,
) -> None:
    alt_url = "https://new.example/guide"
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.9)]},
        confirmations={alt_url: RedirectVerdict(True, "matches")},
    )
    archive = FakeArchive(
        snapshots={URL: None, alt_url: None},
        save_result="https://web.archive.org/web/20261027/https://new.example/guide",
    )
    verifier = make_verifier(
        tmp_path, checker, finder=finder, archive=archive, save_archive=True
    )

    updated = (await verifier.verify([entry()]))[0]

    assert updated.wayback_link == archive.save_result
    assert archive.saves == [alt_url]
    assert verifier.stats.wayback_captures == 1
    assert verifier.cache.get_wayback(alt_url) == archive.save_result


async def test_entry_without_web_link_is_unchanged(tmp_path: object) -> None:
    checker = FakeChecker({})
    finder = FakeFinder()
    verifier = make_verifier(tmp_path, checker, finder=finder)
    unlinked = entry(web_link=None)

    updated = (await verifier.verify([unlinked]))[0]

    assert updated.model_dump() == unlinked.model_dump()
    assert checker.checked == []
    assert finder.discovery_calls == []


async def test_discovery_falls_back_when_first_candidate_is_dead(
    tmp_path: object,
) -> None:
    dead_url = "https://dead.example/guide"
    live_url = "https://live.example/guide"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            dead_url: CheckResult("404", "broken"),
            live_url: live(),
        }
    )
    finder = FakeFinder(
        candidates={
            "guide": [Candidate(dead_url, 0.9), Candidate(live_url, 0.8)],
        },
        confirmations={live_url: RedirectVerdict(True, "same guide")},
    )
    verifier = make_verifier(tmp_path, checker, finder=finder)

    updated = (await verifier.verify([entry()]))[0]

    assert updated.alternative_link == live_url
    assert updated.alternative_probability == 0.8
    assert updated.alternative_note == "confirmed: same guide"
    assert checker.checked == [URL, dead_url, live_url]
    assert verifier.stats.checked == 3


async def test_discovery_rejects_a_page_for_a_different_book(
    tmp_path: object,
) -> None:
    wrong_url = "https://shop.example/wrong-book"
    right_url = "https://shop.example/right-book"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            wrong_url: live(),
            right_url: live(),
        }
    )
    finder = FakeFinder(
        candidates={
            "guide": [Candidate(wrong_url, 0.9), Candidate(right_url, 0.8)],
        },
        confirmations={
            wrong_url: RedirectVerdict(False, "different title"),
            right_url: RedirectVerdict(True, "same guide"),
        },
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link == right_url
    assert updated.alternative_note == "confirmed: same guide"


async def test_discovery_falls_back_when_preferred_is_rejected(
    tmp_path: object,
) -> None:
    old_url = "https://old.example/guide"
    new_url = "https://new.example/guide"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            old_url: live(),
            new_url: live(),
        }
    )
    finder = FakeFinder(
        candidates={"guide": [Candidate(new_url, 0.8)]},
        confirmations={
            old_url: RedirectVerdict(False, "different page"),
            new_url: RedirectVerdict(True, "same guide"),
        },
    )
    cached = entry(alternative_link=old_url, alternative_probability=0.9)

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([cached]))[
        0
    ]

    assert updated.alternative_link == new_url
    assert finder.discovery_calls == ["guide"]
    assert finder.confirm_calls == [("guide", old_url), ("guide", new_url)]


async def test_discovery_keeps_live_candidate_when_confirmation_fails_transiently(
    tmp_path: object,
) -> None:
    alt_url = "https://shop.example/guide"
    checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.9)]},
        confirmations={alt_url: None},
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link == alt_url
    assert updated.alternative_note is None


async def test_discovery_rejects_private_addresses_without_checking(
    tmp_path: object,
) -> None:
    def private_dns(host: str, port: Any) -> list[tuple[Any, ...]]:
        if host == "intranet.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return public_resolver(host, port)

    for private_url in ("http://127.0.0.1/guide", "https://intranet.example/guide"):
        checker = FakeChecker({URL: CheckResult("404", "broken")})
        finder = FakeFinder(candidates={"guide": [Candidate(private_url, 0.9)]})
        verifier = make_verifier(
            tmp_path,
            checker,
            finder=finder,
            cache=CatalogCache(
                path=tmp_path / f"cache-{hash(private_url)}.json",  # type: ignore[operator]
                now=lambda: NOW,
            ),
            resolver=private_dns,
        )

        updated = (await verifier.verify([entry()]))[0]

        assert updated.alternative_link is None
        assert updated.alternative_note == "ignored: private address"
        assert checker.checked == [URL]


@pytest.mark.parametrize(
    "candidate_url",
    [
        "https://example.org/",
        "https://example.org",
    ],
)
async def test_discovery_rejects_site_homepages(
    tmp_path: object, candidate_url: str
) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder(candidates={"guide": [Candidate(candidate_url, 0.9)]})

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link is None
    assert updated.alternative_note == "ignored: site homepage"
    assert checker.checked == [URL]


@pytest.mark.parametrize(
    "candidate_url",
    [
        "https://example.org/guide.jpg",
        "https://example.org/image.aspx?File=guide.jpg&Size=400",
        "https://example.org/scan.PNG",
        "https://www.scribd.com/document/123",
    ],
)
async def test_discovery_rejects_blocked_hosts_and_image_urls(
    tmp_path: object, candidate_url: str
) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder(candidates={"guide": [Candidate(candidate_url, 0.9)]})

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link is None
    assert updated.alternative_note == "ignored: blocked host or image file"
    assert checker.checked == [URL]


@pytest.mark.parametrize(
    ("candidate_url", "expected_note"),
    [
        ("https://user:secret@other.example/guide", "ignored: private address"),
        (URL, "ignored: same as the broken link"),
        (
            "https://www.example.org/guide/?utm_source=x#top",
            "ignored: same as the broken link",
        ),
        ("ftp://example.org/guide", "ignored: not an http link"),
    ],
)
async def test_discovery_rejects_unsafe_or_identical_urls(
    tmp_path: object, candidate_url: str, expected_note: str
) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder(candidates={"guide": [Candidate(candidate_url, 0.9)]})

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link is None
    assert updated.alternative_note == expected_note
    assert checker.checked == [URL]


async def test_discovery_strips_tracking_and_uses_resolved_url(
    tmp_path: object,
) -> None:
    final_url = "https://example.org/final"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            "https://example.org/new": CheckResult("200", "ok", resolved_url=final_url),
        }
    )
    finder = FakeFinder(
        candidates={
            "guide": [
                Candidate(
                    "https://example.org/new?utm_source=gemini#top",
                    0.9,
                    "Official page.",
                )
            ]
        }
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link == final_url
    assert updated.alternative_probability == 0.9
    assert updated.alternative_reasoning == "Official page."
    assert checker.checked == [URL, "https://example.org/new"]


async def test_discovery_rejects_suspicious_redirects(tmp_path: object) -> None:
    hub_url = "https://example.org/hub"
    real_url = "https://example.org/real"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            hub_url: CheckResult(
                "200",
                "ok",
                resolved_url="https://example.org/shop",
                is_suspicious_redirect=True,
            ),
            real_url: live(),
        }
    )
    finder = FakeFinder(
        candidates={"guide": [Candidate(hub_url, 0.9), Candidate(real_url, 0.6)]},
        confirmations={real_url: RedirectVerdict(True, "same guide")},
    )

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link == real_url
    assert updated.alternative_note == "confirmed: same guide"


@pytest.mark.parametrize(
    ("resolved_url", "expected_note"),
    [
        ("http://127.0.0.1/internal", "ignored: private address"),
        ("https://www.scribd.com/document/123", "ignored: blocked host or image file"),
        ("https://example.org/", "ignored: site homepage"),
        (URL, "ignored: same as the broken link"),
    ],
)
async def test_discovery_rejects_unsafe_redirect_targets(
    tmp_path: object, resolved_url: str, expected_note: str
) -> None:
    move_url = "https://example.org/move"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            move_url: CheckResult("200", "ok", resolved_url=resolved_url),
        }
    )
    finder = FakeFinder(candidates={"guide": [Candidate(move_url, 0.9)]})

    updated = (await make_verifier(tmp_path, checker, finder=finder).verify([entry()]))[
        0
    ]

    assert updated.alternative_link is None
    assert updated.alternative_note == expected_note
    assert finder.confirm_calls == []


async def test_transient_discover_failure_does_not_cache(tmp_path: object) -> None:
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder(candidates={"guide": None})
    first = make_verifier(tmp_path, checker, finder=finder)

    updated = (await first.verify([entry()]))[0]
    first.cache.save()

    assert updated.alternative_link is None
    assert updated.verified_at is None

    second_finder = FakeFinder(candidates={"guide": []})
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    await second.verify([entry()])

    assert second_finder.discovery_calls == ["guide"]


async def test_transient_candidate_check_records_retry_note_without_caching(
    tmp_path: object,
) -> None:
    slow_url = "https://slow.example/guide"
    checker = FakeChecker(
        {
            URL: CheckResult("404", "broken"),
            slow_url: CheckResult("503", "blocked", cacheable=False),
        }
    )
    finder = FakeFinder(candidates={"guide": [Candidate(slow_url, 0.9)]})
    first = make_verifier(tmp_path, checker, finder=finder)

    updated = (await first.verify([entry()]))[0]
    first.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == "retry: transient check failure"
    assert updated.verified_at is None

    second_finder = FakeFinder(
        candidates={"guide": [Candidate(slow_url, 0.9)]},
        confirmations={slow_url: RedirectVerdict(True, "recovered")},
    )
    second = make_verifier(
        tmp_path,
        FakeChecker({slow_url: live()}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]

    assert second_finder.discovery_calls == ["guide"]
    assert rerun.alternative_link == slow_url
    assert rerun.alternative_note == "confirmed: recovered"


async def test_rejected_preferred_url_is_cached_even_when_search_fails(
    tmp_path: object,
) -> None:
    hub_url = "https://hub.example/list"
    checker = FakeChecker({URL: CheckResult("404", "broken"), hub_url: live()})
    finder = FakeFinder(
        candidates={"guide": None},
        confirmations={hub_url: RedirectVerdict(False, "category index")},
    )
    first = make_verifier(tmp_path, checker, finder=finder)
    existing = entry(alternative_link=hub_url, alternative_probability=0.9)

    updated = (await first.verify([existing]))[0]
    first.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == "rejected: category index"

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]

    assert rerun.alternative_link is None
    assert rerun.alternative_note == "rejected: category index"
    assert second_finder.discovery_calls == []


async def test_candidate_checks_use_url_cache_and_increment_stats(
    tmp_path: object,
) -> None:
    url_a = "https://example.org/a"
    url_b = "https://example.org/b"
    shared_candidate = "https://shared.example/guide"
    checker = FakeChecker(
        {
            url_a: CheckResult("404", "broken"),
            url_b: CheckResult("404", "broken"),
            shared_candidate: live(),
        }
    )
    finder = FakeFinder(
        candidates={
            "a": [Candidate(shared_candidate, 0.9)],
            "b": [Candidate(shared_candidate, 0.8)],
        },
        confirmations={shared_candidate: RedirectVerdict(True, "matches")},
    )
    verifier = make_verifier(tmp_path, checker, finder=finder, concurrency=1)

    result = await verifier.verify(
        [entry("a", web_link=url_a), entry("b", web_link=url_b)]
    )

    assert [item.alternative_link for item in result] == [
        shared_candidate,
        shared_candidate,
    ]
    assert checker.checked == [url_a, shared_candidate, url_b]
    assert verifier.stats.checked == 3
    assert verifier.stats.cache_hits == 1


async def test_offline_guard_aborts_during_candidate_check(tmp_path: object) -> None:
    alt_url = "https://new.example/guide"

    class CandidateAbortChecker(FakeChecker):
        def should_abort(self) -> bool:
            return len(self.checked) >= 2

    checker = CandidateAbortChecker(
        {
            URL: CheckResult("404", "broken"),
            alt_url: CheckResult("connect_error", "broken", cacheable=False),
        }
    )
    finder = FakeFinder(candidates={"guide": [Candidate(alt_url, 0.9)]})
    verifier = make_verifier(tmp_path, checker, finder=finder)

    with pytest.raises(OfflineRunError):
        await verifier.verify([entry()])


async def test_reconfirm_drops_dead_cached_alternative_with_rejected_note(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_check(URL, CheckResult("404", "broken"))
    cache.update_entry(
        "guide",
        URL,
        alternative_url=alt_url,
        alternative_probability=0.9,
        alternative_checked=True,
        alternative_confirmed=False,
    )
    cache.save()

    finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({alt_url: CheckResult("404", "broken")}),
        finder=finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]
    second.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == "rejected: link returned 404"
    assert finder.confirm_calls == []


async def test_reconfirm_rejects_redirect_to_private_address(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.put_check(URL, CheckResult("404", "broken"))
    cache.update_entry(
        "guide",
        URL,
        alternative_url=alt_url,
        alternative_probability=0.9,
        alternative_checked=True,
        alternative_confirmed=False,
    )
    cache.save()

    finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker(
            {
                alt_url: CheckResult(
                    "200", "ok", resolved_url="http://127.0.0.1/internal"
                )
            }
        ),
        finder=finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]

    assert updated.alternative_link is None
    assert updated.alternative_note == "ignored: private address"
    assert finder.confirm_calls == []


async def test_reconfirm_transient_failure_records_retry_note_without_caching(
    tmp_path: object,
) -> None:
    alt_url = "https://publisher.example/guide"
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.update_entry(
        "guide",
        URL,
        alternative_url=alt_url,
        alternative_probability=0.9,
        alternative_checked=True,
        alternative_confirmed=False,
    )
    cache.put_check(URL, CheckResult("404", "broken"))
    cache.save()

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({alt_url: CheckResult("503", "blocked", cacheable=False)}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    updated = (await second.verify([entry()]))[0]
    second.cache.save()

    assert updated.alternative_link == alt_url
    assert updated.alternative_note == "retry: transient check failure"
    assert second_finder.confirm_calls == []

    third_finder = FakeFinder(
        confirmations={alt_url: RedirectVerdict(True, "verified after retry")}
    )
    third = make_verifier(
        tmp_path,
        FakeChecker({alt_url: live()}),
        finder=third_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    confirmed = (await third.verify([entry()]))[0]

    assert confirmed.alternative_link == alt_url
    assert confirmed.alternative_note == "confirmed: verified after retry"
    assert third_finder.confirm_calls == [("guide", alt_url)]


@pytest.mark.parametrize(
    ("bad_url", "expected_note"),
    [
        ("https://example.org/", "ignored: site homepage"),
        ("https://example.org/scan.jpg", "ignored: blocked host or image file"),
        ("https://www.scribd.com/document/123", "ignored: blocked host or image file"),
        (URL, "ignored: same as the broken link"),
        ("ftp://example.org/guide", "ignored: not an http link"),
    ],
)
async def test_confirmed_cached_alternative_is_filtered_by_pure_url_policy(
    tmp_path: object, bad_url: str, expected_note: str
) -> None:
    cache = CatalogCache(
        path=tmp_path / "cache.json",  # type: ignore[operator]
        now=lambda: NOW,
    )
    cache.update_entry(
        "guide",
        URL,
        alternative_url=bad_url,
        alternative_probability=0.9,
        alternative_note="confirmed: legacy match",
        alternative_checked=True,
        alternative_confirmed=True,
    )
    checker = FakeChecker({URL: CheckResult("404", "broken")})
    finder = FakeFinder()
    verifier = make_verifier(tmp_path, checker, finder=finder, cache=cache)

    updated = (await verifier.verify([entry()]))[0]
    verifier.cache.save()

    assert updated.alternative_link is None
    assert updated.alternative_note == expected_note
    assert checker.checked == [URL]
    assert finder.confirm_calls == []
    assert finder.discovery_calls == []

    second_finder = FakeFinder()
    second = make_verifier(
        tmp_path,
        FakeChecker({}),
        finder=second_finder,
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    rerun = (await second.verify([entry()]))[0]

    assert rerun.alternative_link is None
    assert rerun.alternative_note == expected_note
    assert second_finder.discovery_calls == []


async def test_refresh_forces_fresh_check_and_preserves_wayback_links(
    tmp_path: object,
) -> None:
    snapshot = "https://web.archive.org/web/2020/https://example.org/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: live()}),
        archive=FakeArchive({URL: snapshot}),
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()
    assert initial.wayback_link == snapshot

    moved_url = "https://example.org/guide-v2"
    refresh_checker = FakeChecker({URL: live(moved_url)})
    refreshed_verifier = make_verifier(
        tmp_path,
        refresh_checker,
        archive=FakeArchive({}),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
        refresh=True,
    )
    refreshed = (await refreshed_verifier.verify([entry()]))[0]
    refreshed_verifier.cache.save()

    assert refresh_checker.checked == [URL]
    assert refreshed_verifier.stats.checked == 1
    assert refreshed_verifier.stats.cache_hits == 0
    assert refreshed.web_link_resolved == moved_url
    assert refreshed.wayback_link == snapshot

    cached_verifier = make_verifier(
        tmp_path,
        FakeChecker({}),
        archive=FakeArchive({}),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
    )
    reused = (await cached_verifier.verify([entry()]))[0]

    assert cached_verifier.stats.checked == 0
    assert cached_verifier.stats.cache_hits == 1
    assert reused.web_link_resolved == moved_url
    assert reused.wayback_link == snapshot


async def test_refresh_preserves_alternative_wayback_link(tmp_path: object) -> None:
    alt_url = "https://new.example/guide"
    alt_snapshot = "https://web.archive.org/web/2026/https://new.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: RedirectVerdict(True, "matches")},
        ),
        archive=FakeArchive({URL: None, alt_url: alt_snapshot}),
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()
    assert initial.wayback_link == alt_snapshot

    refresh_checker = FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()})
    refresh_finder = FakeFinder(
        candidates={"guide": [Candidate(alt_url, 0.9)]},
        confirmations={alt_url: RedirectVerdict(True, "still matches")},
    )
    refreshed = (
        await make_verifier(
            tmp_path,
            refresh_checker,
            finder=refresh_finder,
            archive=FakeArchive({}),
            cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),  # type: ignore[operator]
            refresh=True,
        ).verify([initial])
    )[0]

    assert refresh_checker.checked == [URL, alt_url]
    assert refreshed.alternative_link == alt_url
    assert refreshed.wayback_link == alt_snapshot


async def test_refresh_clears_stale_redirect_verdict_when_link_becomes_404(
    tmp_path: Path,
) -> None:
    alt_url = "https://new.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker(
            {URL: live("https://other.example/", suspicious=True), alt_url: live()}
        ),
        finder=FakeFinder(
            redirects=[RedirectVerdict(False, "Generic homepage.")],
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: RedirectVerdict(True, "matches")},
        ),
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()
    assert initial.web_link_status == "redirect_mismatch"

    refreshed_verifier = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken")}),
        finder=FakeFinder(),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),
        refresh=True,
    )
    refreshed = (await refreshed_verifier.verify([initial]))[0]
    refreshed_verifier.cache.save()

    assert refreshed.web_link_state == "broken"
    assert refreshed.web_link_status == "404"
    reloaded = CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW)
    record = reloaded.get_entry("guide", URL)
    assert record is not None
    assert record.redirect_is_match is None
    assert record.redirect_reason is None


async def test_refresh_clears_stale_redirect_and_alternative_when_link_recovers(
    tmp_path: Path,
) -> None:
    alt_url = "https://new.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker(
            {URL: live("https://other.example/", suspicious=True), alt_url: live()}
        ),
        finder=FakeFinder(
            redirects=[RedirectVerdict(False, "Generic homepage.")],
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: RedirectVerdict(True, "matches")},
        ),
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()
    assert initial.web_link_status == "redirect_mismatch"

    refreshed_verifier = make_verifier(
        tmp_path,
        FakeChecker({URL: live()}),
        finder=FakeFinder(),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),
        refresh=True,
    )
    refreshed = (await refreshed_verifier.verify([initial]))[0]
    refreshed_verifier.cache.save()

    assert refreshed.web_link_state == "ok"
    assert refreshed.alternative_link is None
    reloaded = CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW)
    assert reloaded.get_entry("guide", URL) is None
    payload = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert payload["entries"] == {}


async def test_recovery_without_alternative_clears_stale_redirect_verdict(
    tmp_path: Path,
) -> None:
    current = NOW
    cache = CatalogCache(path=tmp_path / "cache.json", ttl_days=30, now=lambda: current)
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: live("https://other.example/", suspicious=True)}),
        finder=FakeFinder(redirects=[RedirectVerdict(False, "Generic homepage.")]),
        cache=cache,
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()
    assert initial.web_link_status == "redirect_mismatch"

    # Simulate URL-tier cache invalidation before Entry-tier expires,
    # then a non-redirecting 200 OK recovery without --refresh.
    reloaded = CatalogCache.load(
        tmp_path / "cache.json",
        ttl_days=30,
        now=lambda: current,
    )
    reloaded.urls.clear()
    second = make_verifier(
        tmp_path,
        FakeChecker({URL: live()}),
        finder=FakeFinder(),
        cache=reloaded,
    )
    recovered = (await second.verify([initial]))[0]
    second.cache.save()

    assert recovered.web_link_state == "ok"
    assert recovered.alternative_link is None
    assert recovered.alternative_note is None
    final_cache = CatalogCache.load(tmp_path / "cache.json", now=lambda: current)
    assert final_cache.get_entry("guide", URL) is None
    payload = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert payload["entries"] == {}


async def test_refresh_redirect_with_transient_discovery_keeps_cached_alternative(
    tmp_path: Path,
) -> None:
    alt_url = "https://new.example/guide"
    first = make_verifier(
        tmp_path,
        FakeChecker({URL: CheckResult("404", "broken"), alt_url: live()}),
        finder=FakeFinder(
            candidates={"guide": [Candidate(alt_url, 0.9)]},
            confirmations={alt_url: RedirectVerdict(True, "matches")},
        ),
    )
    initial = (await first.verify([entry()]))[0]
    first.cache.save()

    second = make_verifier(
        tmp_path,
        FakeChecker(
            {
                URL: live("https://other.example/", suspicious=True),
                alt_url: CheckResult("503", "blocked", cacheable=False),
            }
        ),
        finder=FakeFinder(
            redirects=[RedirectVerdict(False, "Generic homepage.")],
            candidates={"guide": None},
        ),
        cache=CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW),
        refresh=True,
    )
    updated = (await second.verify([initial]))[0]
    second.cache.save()

    assert updated.web_link_status == "redirect_mismatch"
    assert updated.alternative_link == alt_url
    reloaded = CatalogCache.load(tmp_path / "cache.json", now=lambda: NOW)
    record = reloaded.get_entry("guide", URL)
    assert record is not None
    assert record.redirect_is_match is False
    assert record.redirect_reason == "Generic homepage."
    assert record.alternative_url == alt_url
    assert record.alternative_confirmed is True


async def test_gemini_semaphore_is_not_held_during_candidate_http_checks(
    tmp_path: object,
) -> None:
    broken_url = "https://example.org/broken"
    redirect_url = "https://example.org/redirect"
    candidate_url = "https://slow.example/guide"
    candidate_started = asyncio.Event()
    allow_candidate = asyncio.Event()

    class CoordinatedChecker(FakeChecker):
        async def check(
            self, url: str, *, entry_title: str | None = None
        ) -> CheckResult:
            self.checked.append(url)
            if url == candidate_url:
                candidate_started.set()
                await allow_candidate.wait()
                return live()
            return self.results[url]

    class CoordinatedFinder(FakeFinder):
        async def verify_redirect(self, **kwargs: object) -> RedirectVerdict | None:
            await candidate_started.wait()
            allow_candidate.set()
            return RedirectVerdict(True, "matched")

    checker = CoordinatedChecker(
        {
            broken_url: CheckResult("404", "broken"),
            redirect_url: live("https://other.example/guide", suspicious=True),
        }
    )
    finder = CoordinatedFinder(
        candidates={"a": [Candidate(candidate_url, 0.9)]},
        confirmations={candidate_url: RedirectVerdict(True, "same guide")},
    )
    verifier = make_verifier(
        tmp_path,
        checker,
        finder=finder,
        concurrency=2,
        gemini_concurrency=1,
    )

    result = await asyncio.wait_for(
        verifier.verify(
            [
                entry("a", web_link=broken_url),
                entry("b", web_link=redirect_url),
            ]
        ),
        timeout=1.0,
    )

    assert [item.web_link_state for item in result] == ["broken", "ok"]
    assert result[0].alternative_link == candidate_url


def test_verify_config_resolves_model_names() -> None:
    default = VerifyConfig()
    assert default.verification_model_name == "gemini-3.1-flash-lite"
    assert default.discovery_model_name == "gemini-3.1-flash-lite"

    shared = VerifyConfig(model="gemini-3.5-flash")
    assert shared.verification_model_name == "gemini-3.5-flash"
    assert shared.discovery_model_name == "gemini-3.5-flash"

    split = VerifyConfig(
        model="gemini-3.5-flash",
        verification_model="gemini-3.1-flash-lite",
        discovery_model="gemini-3.8-flash",
    )
    assert split.verification_model_name == "gemini-3.1-flash-lite"
    assert split.discovery_model_name == "gemini-3.8-flash"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"sample": 2, "limit": 1}, "--sample and --limit are mutually exclusive"),
        ({"sample": 0}, "--sample must be at least 1"),
        ({"limit": 0}, "--limit must be at least 1"),
        ({"min_prob": -0.1}, "--min-prob must be between 0 and 1"),
        ({"min_prob": 1.1}, "--min-prob must be between 0 and 1"),
        ({"concurrency": 0}, "--concurrency must be at least 1"),
        ({"wayback_concurrency": 0}, "--wayback-concurrency must be at least 1"),
        ({"gemini_concurrency": 0}, "--gemini-concurrency must be at least 1"),
        ({"timeout": 0.0}, "--timeout must be positive"),
        ({"timeout": -5.0}, "--timeout must be positive"),
        ({"cache_ttl_days": -1}, "--cache-ttl-days must not be negative"),
        ({"max_searches": -1}, "--max-searches must not be negative"),
    ],
)
def test_verify_config_validate_rejects_invalid_options(
    overrides: dict[str, Any], expected: str
) -> None:
    config = VerifyConfig(**overrides)
    assert config.validate() == expected


def test_verify_config_validate_accepts_boundary_values() -> None:
    config = VerifyConfig(min_prob=0.0, cache_ttl_days=0, max_searches=0, timeout=0.1)
    assert config.validate() is None


def test_verify_config_select_targets_filters_broken_before_limit_and_sample() -> None:
    entries = [
        entry("ok-1", web_link_state="ok"),
        entry("broken-1", web_link_state="broken"),
        entry("broken-2", web_link_state="broken"),
        entry("broken-3", web_link_state="broken"),
    ]

    limited = VerifyConfig(only_broken=True, limit=2).select_targets(entries)
    assert [item.id for item in limited] == ["broken-1", "broken-2"]

    sampled = VerifyConfig(only_broken=True, sample=2, seed=7).select_targets(entries)
    assert len(sampled) == 2
    assert all(item.web_link_state == "broken" for item in sampled)


class CloseableChecker(FakeChecker):
    def __init__(self, results: dict[str, CheckResult], *, abort: bool = False) -> None:
        super().__init__(results, abort=abort)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class CloseableArchive(FakeArchive):
    def __init__(self, snapshots: dict[str, str | None] | None = None) -> None:
        super().__init__(snapshots)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class RunFakeGenerator:
    def __init__(self, script: list[GenerationResult]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, bool]] = []

    async def generate(
        self, prompt: str, *, model: str, json_mode: bool, search: bool
    ) -> GenerationResult:
        self.calls.append((model, search))
        return self.script.pop(0)


async def test_run_verification_rejects_invalid_config(tmp_path: Path) -> None:
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    config = VerifyConfig(sample=2, limit=1, cache_file=cache_file, cost_file=cost_file)

    with pytest.raises(ValueError, match="mutually exclusive"):
        await run_verification([entry()], config)

    assert not cache_file.exists()
    assert not cost_file.exists()


async def test_run_verification_selects_merges_saves_cache_closes_and_tracks_cost(
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    entries = [
        entry("a", web_link="https://example.org/a"),
        entry("b", web_link="https://example.org/b"),
        entry("c", web_link="https://example.org/c"),
    ]
    checker = CloseableChecker(
        {
            "https://example.org/a": live(),
            "https://example.org/b": live(),
        }
    )
    archive = CloseableArchive()
    config = VerifyConfig(
        command="verify",
        limit=2,
        cache_file=cache_file,
        cost_file=cost_file,
    )

    merged, stats = await run_verification(
        entries,
        config,
        deps=VerifyDeps(checker=checker, finder=FakeFinder(), wayback=archive),
    )

    assert [item.web_link_state for item in merged] == ["ok", "ok", None]
    assert stats.checked == 2
    assert stats.cost_summary is not None
    assert "idsignpost verify cost breakdown:" in stats.cost_summary
    assert checker.closed is True
    assert archive.closed is True
    assert cache_file.exists()
    assert len(CatalogCache.load(cache_file).urls) == 2
    cost_payload = json.loads(cost_file.read_text(encoding="utf-8"))
    assert len(cost_payload["runs"]) == 1
    assert cost_payload["runs"][0]["command"] == "verify"


async def test_run_verification_saves_cache_and_closes_on_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    first_url = "https://example.org/first"
    second_url = "https://example.org/second"

    class InterruptingChecker(CloseableChecker):
        async def check(
            self, url: str, *, entry_title: str | None = None
        ) -> CheckResult:
            self.checked.append(url)
            if url == second_url:
                raise KeyboardInterrupt
            return live()

    checker = InterruptingChecker({})
    archive = CloseableArchive()
    config = VerifyConfig(
        concurrency=1,
        cache_file=cache_file,
        cost_file=cost_file,
    )

    with pytest.raises(KeyboardInterrupt):
        await run_verification(
            [
                entry("first", web_link=first_url),
                entry("second", web_link=second_url),
            ],
            config,
            deps=VerifyDeps(checker=checker, finder=FakeFinder(), wayback=archive),
        )

    assert checker.closed is True
    assert archive.closed is True
    assert cache_file.exists()
    assert CatalogCache.load(cache_file).get_check(first_url) is not None
    assert not cost_file.exists()


async def test_run_verification_skips_cache_save_on_offline_abort(
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    checker = CloseableChecker({}, abort=True)
    archive = CloseableArchive()
    config = VerifyConfig(cache_file=cache_file, cost_file=cost_file)

    with pytest.raises(OfflineRunError):
        await run_verification(
            [entry()],
            config,
            deps=VerifyDeps(checker=checker, finder=FakeFinder(), wayback=archive),
        )

    assert checker.closed is True
    assert archive.closed is True
    assert not cache_file.exists()
    assert not cost_file.exists()


async def test_run_verification_wires_models_and_max_searches_to_gemini_finder(
    tmp_path: Path,
) -> None:
    cache_file = tmp_path / "cache.json"
    cost_file = tmp_path / "costs.json"
    redirect_url = "https://example.org/moved"
    broken_one = "https://example.org/broken-1"
    broken_two = "https://example.org/broken-2"
    alt_url = "https://replacement.example/guide"

    checker = CloseableChecker(
        {
            redirect_url: live("https://other.example/guide", suspicious=True),
            broken_one: CheckResult("404", "broken"),
            broken_two: CheckResult("404", "broken"),
            alt_url: live(),
        }
    )
    generator = RunFakeGenerator(
        [
            GenerationResult(
                text='{"is_match": true, "reasoning": "Moved host."}',
                prompt_tokens=100,
                output_tokens=20,
            ),
            GenerationResult(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "url": alt_url,
                                "probability": 0.9,
                                "reasoning": "Official page.",
                            }
                        ]
                    }
                ),
                prompt_tokens=200,
                output_tokens=50,
                search_queries=1,
            ),
            GenerationResult(
                text='{"is_match": true, "reasoning": "Matches guide."}',
                prompt_tokens=150,
                output_tokens=25,
            ),
        ]
    )
    config = VerifyConfig(
        command="run",
        concurrency=1,
        cache_file=cache_file,
        cost_file=cost_file,
        model="gemini-3.5-flash",
        verification_model="gemini-3.1-flash-lite",
        discovery_model="gemini-3.8-flash",
        max_searches=1,
    )

    merged, stats = await run_verification(
        [
            entry("redir", web_link=redirect_url),
            entry("b1", web_link=broken_one),
            entry("b2", web_link=broken_two),
        ],
        config,
        deps=VerifyDeps(
            checker=checker,
            wayback=CloseableArchive(),
            generator=generator,
            resolver=public_resolver,
        ),
    )

    assert generator.calls == [
        ("gemini-3.1-flash-lite", False),
        ("gemini-3.8-flash", True),
        ("gemini-3.1-flash-lite", False),
    ]
    assert merged[0].web_link_state == "ok"
    assert merged[1].alternative_link == alt_url
    assert merged[2].alternative_link is None
    assert merged[2].alternative_note == "search failed"
    assert stats.cost_summary is not None
    assert "idsignpost run cost breakdown:" in stats.cost_summary
    cost_payload = json.loads(cost_file.read_text(encoding="utf-8"))
    assert cost_payload["runs"][0]["calls"] == 3
    assert cost_payload["runs"][0]["search_queries"] == 1


async def test_run_verification_save_archive_requires_ia_keys_by_default(
    tmp_path: Path,
) -> None:
    config = VerifyConfig(
        save_archive=True,
        cache_file=tmp_path / "cache.json",
        cost_file=tmp_path / "costs.json",
    )

    with pytest.raises(ValueError, match="IA_ACCESS_KEY and IA_SECRET_KEY"):
        await run_verification(
            [entry()],
            config,
            deps=VerifyDeps(
                checker=CloseableChecker({URL: live()}),
                finder=FakeFinder(),
            ),
        )


async def test_run_verification_wires_ia_keys_and_no_ia_keys_fallback(
    tmp_path: Path,
) -> None:
    from idsignpost.wayback import AVAILABILITY_URL, SAVE_URL, SPN2_SAVE_URL

    with respx.mock:
        respx.get(AVAILABILITY_URL).mock(
            return_value=httpx.Response(200, json={"archived_snapshots": {}})
        )
        spn2_route = respx.post(SPN2_SAVE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "success",
                    "timestamp": "20261123000000",
                    "original_url": URL,
                },
            )
        )
        anon_route = respx.get(f"{SAVE_URL}{URL}").mock(
            return_value=httpx.Response(
                200,
                headers={
                    "content-location": f"/web/20261123000001/{URL}",
                },
            )
        )

        auth_config = VerifyConfig(
            save_archive=True,
            ia_access_key="ia-access",
            ia_secret_key="ia-secret",
            cache_file=tmp_path / "auth-cache.json",
            cost_file=tmp_path / "auth-costs.json",
        )
        auth_merged, _ = await run_verification(
            [entry()],
            auth_config,
            deps=VerifyDeps(
                checker=CloseableChecker({URL: live()}),
                finder=FakeFinder(),
            ),
        )
        assert (
            auth_merged[0].wayback_link
            == f"https://web.archive.org/web/20261123000000/{URL}"
        )
        assert spn2_route.call_count == 1
        assert anon_route.call_count == 0

        fallback_config = VerifyConfig(
            save_archive=True,
            no_ia_keys=True,
            ia_access_key="ia-access",
            ia_secret_key="ia-secret",
            cache_file=tmp_path / "anon-cache.json",
            cost_file=tmp_path / "anon-costs.json",
        )
        anon_merged, _ = await run_verification(
            [entry()],
            fallback_config,
            deps=VerifyDeps(
                checker=CloseableChecker({URL: live()}),
                finder=FakeFinder(),
            ),
        )
        assert (
            anon_merged[0].wayback_link
            == f"https://web.archive.org/web/20261123000001/{URL}"
        )
        assert anon_route.call_count == 1

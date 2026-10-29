"""Verification pipeline: HTTP liveness, Gemini verdicts, Wayback, and cache."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from idsignpost.cache import CatalogCache, EntryRecord, UrlRecord
from idsignpost.gemini_finder import (
    Alternative,
    DiscoveryOutcome,
    RedirectVerdict,
    is_unusable_alternative,
)
from idsignpost.http_checker import CheckResult
from idsignpost.models import (
    VERIFICATION_FIELDS,
    CatalogEntry,
    LinkState,
    ensure_unique_ids,
)
from idsignpost.urls import cache_key

DEFAULT_CONCURRENCY = 10
DEFAULT_GEMINI_CONCURRENCY = 3
SHARED_ALTERNATIVE_LIMIT = 3


class OfflineRunError(RuntimeError):
    """Raised when every check so far failed at the network level."""


class Checker(Protocol):
    async def check(
        self, url: str, *, entry_title: str | None = None
    ) -> CheckResult: ...

    def should_abort(self) -> bool: ...


class Finder(Protocol):
    async def verify_redirect(
        self,
        *,
        title: str,
        authors: str | None,
        description: str | None,
        web_link: str,
        resolved_url: str | None,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None = None,
    ) -> RedirectVerdict | None: ...

    async def find_alternative(
        self,
        entry: CatalogEntry,
        *,
        checker: Checker,
        preferred: Alternative | None = None,
    ) -> DiscoveryOutcome: ...

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None,
    ) -> RedirectVerdict | None: ...


class Archive(Protocol):
    async def lookup(self, url: str) -> str | None: ...

    async def save(self, url: str) -> str | None: ...


@dataclass
class VerifyStats:
    """Counts for the run summary."""

    checked: int = 0
    cache_hits: int = 0
    alternatives: int = 0
    wayback_captures: int = 0
    states: dict[str, int] = field(default_factory=dict)
    cost_summary: str | None = None


@dataclass(frozen=True, slots=True)
class _UrlFacts:
    result: CheckResult
    transient: bool
    checked: bool
    wayback: str | None = None


@dataclass(frozen=True, slots=True)
class _Verdict:
    state: LinkState | None
    status: str | None
    resolved_url: str | None
    alternative: Alternative | None
    transient: bool = False
    keep: bool = False
    note: str | None = None
    fresh: bool = False
    entry_record: EntryRecord | None = None
    write_entry: bool = False


def _alternative_fields(entry: CatalogEntry) -> Alternative | None:
    if not entry.alternative_link:
        return None
    return Alternative(
        url=entry.alternative_link,
        probability=entry.alternative_probability or 0.0,
        reasoning=entry.alternative_reasoning,
    )


class Verifier:
    """Verify each URL once, then apply per-entry verdicts."""

    def __init__(
        self,
        *,
        cache: CatalogCache,
        checker: Checker,
        finder: Finder,
        wayback: Archive,
        concurrency: int = DEFAULT_CONCURRENCY,
        gemini_concurrency: int = DEFAULT_GEMINI_CONCURRENCY,
        save_archive: bool = False,
        refresh: bool = False,
    ) -> None:
        self.cache = cache
        self.checker = checker
        self.finder = finder
        self.wayback = wayback
        self.save_archive = save_archive
        self.refresh = refresh
        self.stats = VerifyStats()
        self._http = asyncio.Semaphore(concurrency)
        self._gemini = asyncio.Semaphore(gemini_concurrency)

    async def verify(self, entries: Sequence[CatalogEntry]) -> list[CatalogEntry]:
        """Return the catalog with verification fields refreshed."""
        ensure_unique_ids(entries)
        self.stats = VerifyStats()
        groups: dict[str, list[CatalogEntry]] = {}
        for entry in entries:
            if entry.web_link:
                groups.setdefault(cache_key(entry.web_link), []).append(entry)

        updated: dict[str, CatalogEntry] = {}
        tasks = [
            asyncio.create_task(self._verify_group(members))
            for members in groups.values()
        ]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                error = task.exception()
                if error is not None:
                    for other in pending:
                        other.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise error
                for entry in task.result():
                    updated[entry.id] = entry

        result = [updated.get(entry.id, entry) for entry in entries]
        result = self._drop_shared_alternatives(result)
        counts = Counter(entry.web_link_state or "unverified" for entry in result)
        self.stats.states = dict(counts)
        self.stats.alternatives = sum(
            1 for entry in result if entry.alternative_link is not None
        )
        return result

    def _drop_shared_alternatives(
        self, entries: list[CatalogEntry]
    ) -> list[CatalogEntry]:
        """Reject a URL handed to three or more entries as a category hub."""
        counts = Counter(
            entry.alternative_link for entry in entries if entry.alternative_link
        )
        shared = {
            url for url, count in counts.items() if count >= SHARED_ALTERNATIVE_LIMIT
        }
        if not shared:
            return entries

        cleaned: list[CatalogEntry] = []
        for entry in entries:
            if entry.alternative_link not in shared:
                cleaned.append(entry)
                continue
            orig_wayback = (
                self.cache.get_wayback(entry.web_link) if entry.web_link else None
            )
            entry = entry.model_copy(
                update={
                    "alternative_link": None,
                    "alternative_probability": None,
                    "alternative_reasoning": None,
                    "alternative_note": (
                        "dropped: URL shared by three or more entries"
                    ),
                    "wayback_link": orig_wayback,
                }
            )
            record = (
                self.cache.get_entry(entry.id, entry.web_link)
                if entry.web_link
                else None
            )
            if record is not None:
                record.drop_alternative("dropped: URL shared by three or more entries")
                self.cache.put_entry(entry.id, entry.web_link or "", record)
            cleaned.append(entry)
        return cleaned

    async def _verify_group(self, members: list[CatalogEntry]) -> list[CatalogEntry]:
        entry = members[0]
        url = entry.web_link or ""
        async with self._http:
            facts = await self._facts(url, entry.title)
            verdicts = await asyncio.gather(
                *(self._verdict(member, facts) for member in members)
            )
            wayback_link = await self._wayback(url, facts, verdicts)
            wayback_links = [
                wayback_link
                if wayback_link is not None
                or verdict.alternative is None
                or verdict.transient
                else await self._alternative_wayback(verdict.alternative.url)
                for verdict in verdicts
            ]
        return [
            self._apply(member, verdict, facts, member_wayback)
            for member, verdict, member_wayback in zip(
                members, verdicts, wayback_links, strict=True
            )
        ]

    async def _facts(self, url: str, title: str) -> _UrlFacts:
        record = None if self.refresh else self.cache.get_url(url)
        if record is not None and record.state is not None:
            self.stats.cache_hits += 1
            return _UrlFacts(
                result=CheckResult(
                    status=record.status or "",
                    state=record.state,
                    resolved_url=record.resolved_url,
                    is_suspicious_redirect=record.is_suspicious_redirect,
                    page_title=record.page_title,
                    page_h1=record.page_h1,
                    page_snippet=record.page_snippet,
                ),
                transient=False,
                checked=False,
                wayback=record.wayback_url,
            )

        result = await self.checker.check(url, entry_title=title)
        self.stats.checked += 1
        if self.checker.should_abort():
            raise OfflineRunError(
                f"every check failed at the network level after {self.stats.checked} attempts"
            )
        self.cache.put_url(
            url,
            UrlRecord(
                status=result.status,
                state=result.state,
                resolved_url=result.resolved_url,
                is_suspicious_redirect=result.is_suspicious_redirect,
                page_title=result.page_title,
                page_h1=result.page_h1,
                page_snippet=result.page_snippet,
                wayback_url=record.wayback_url if record else None,
            ),
            cacheable=result.cacheable,
        )
        return _UrlFacts(
            result=result,
            transient=not result.cacheable,
            checked=True,
            wayback=record.wayback_url if record else None,
        )

    async def _verdict(self, entry: CatalogEntry, facts: _UrlFacts) -> _Verdict:
        result = facts.result
        record = (
            None
            if self.refresh or not entry.web_link
            else self.cache.get_entry(entry.id, entry.web_link)
        )
        previous = _alternative_fields(entry)

        if facts.transient:
            if entry.web_link_state is not None:
                return _Verdict(
                    state=entry.web_link_state,
                    status=entry.web_link_status,
                    resolved_url=entry.web_link_resolved,
                    alternative=previous,
                    transient=True,
                )
            return _Verdict(
                state=result.state,
                status=result.status,
                resolved_url=result.resolved_url,
                alternative=previous,
                transient=True,
            )

        state: LinkState | None = result.state
        status = result.status
        resolved = result.resolved_url
        verdict: RedirectVerdict | None = None
        verdict_cached = False
        fresh = facts.checked

        if state == "ok" and result.is_suspicious_redirect:
            if record is not None and record.redirect_is_match is not None:
                verdict = RedirectVerdict(
                    record.redirect_is_match, record.redirect_reason
                )
                verdict_cached = True
            else:
                verdict = await self._verify_redirect(entry, facts, resolved)
                if verdict is None:
                    return _Verdict(
                        state=entry.web_link_state,
                        status=entry.web_link_status,
                        resolved_url=entry.web_link_resolved,
                        alternative=previous,
                        keep=True,
                    )
                fresh = True
            if not verdict.is_match:
                state, status = "broken", "redirect_mismatch"

        alternative = previous
        alternative_confirmed = False
        note: str | None = None
        discovered = False
        discovery_transient = False
        cached_rejected = False
        cached_confirmed = False
        if state == "broken":
            if record is not None and record.alternative_checked:
                cached_url = record.alternative_url
                if cached_url and is_unusable_alternative(cached_url):
                    alternative = None
                    cached_rejected = True
                    note = "dropped: blocked host or image file"
                elif cached_url and not record.alternative_confirmed:
                    alternative, alternative_confirmed, note = await self._reconfirm(
                        entry, record
                    )
                    cached_confirmed = alternative_confirmed
                    cached_rejected = alternative is None
                else:
                    alternative = (
                        Alternative(
                            cached_url,
                            record.alternative_probability or 0.0,
                            record.alternative_reasoning,
                        )
                        if cached_url
                        else None
                    )
                    alternative_confirmed = bool(cached_url)
                    note = record.alternative_note
            else:
                discovered = True
                outcome = await self._discover(entry)
                if outcome.transient:
                    discovery_transient = True
                    alternative = previous
                    note = entry.alternative_note
                else:
                    alternative = outcome.alternative
                    alternative_confirmed = outcome.confirmed
                    note = outcome.note
                    fresh = True
        else:
            alternative = None

        write_entry = discovered and not discovery_transient
        if verdict is not None and not verdict_cached:
            write_entry = True
        if cached_rejected or cached_confirmed:
            write_entry = True
        if (
            state != "broken"
            and record is not None
            and record.alternative_url is not None
        ):
            write_entry = True

        entry_record = None
        if write_entry:
            entry_record = EntryRecord(
                redirect_is_match=(
                    verdict.is_match
                    if verdict is not None
                    else (record.redirect_is_match if record else None)
                ),
                redirect_reason=(
                    verdict.reasoning
                    if verdict is not None
                    else (record.redirect_reason if record else None)
                ),
                alternative_url=alternative.url if alternative else None,
                alternative_probability=(
                    alternative.probability if alternative else None
                ),
                alternative_reasoning=(alternative.reasoning if alternative else None),
                alternative_note=note,
                alternative_checked=(
                    (record.alternative_checked if record else False) or discovered
                ),
                alternative_confirmed=(
                    alternative_confirmed if alternative is not None else False
                ),
            )

        return _Verdict(
            state=state,
            status=status,
            resolved_url=resolved,
            alternative=alternative,
            note=note,
            fresh=fresh and not discovery_transient,
            entry_record=entry_record,
            write_entry=write_entry,
        )

    async def _verify_redirect(
        self, entry: CatalogEntry, facts: _UrlFacts, resolved: str | None
    ) -> RedirectVerdict | None:
        async with self._gemini:
            return await self.finder.verify_redirect(
                title=entry.title,
                authors=entry.authors,
                description=entry.description,
                web_link=entry.web_link or "",
                resolved_url=resolved,
                page_title=facts.result.page_title,
                page_h1=facts.result.page_h1,
                page_snippet=facts.result.page_snippet,
            )

    async def _discover(self, entry: CatalogEntry) -> DiscoveryOutcome:
        async with self._gemini:
            return await self.finder.find_alternative(
                entry, checker=self.checker, preferred=_alternative_fields(entry)
            )

    async def _reconfirm(
        self, entry: CatalogEntry, record: EntryRecord
    ) -> tuple[Alternative | None, bool, str | None]:
        """Recheck a cached alternative that was stored before confirmation."""
        url = record.alternative_url
        if not url:
            return None, False, None
        cached = Alternative(
            url,
            record.alternative_probability or 0.0,
            record.alternative_reasoning,
        )
        facts = await self._facts(url, entry.title)
        if facts.transient:
            return cached, False, record.alternative_note
        if facts.result.state != "ok" or facts.result.is_suspicious_redirect:
            return None, False, "dropped: stored alternative is no longer live"
        resolved = facts.result.resolved_url or url
        verdict = await self._confirm(
            entry,
            resolved,
            facts.result.page_title,
            facts.result.page_h1,
            facts.result.page_snippet,
        )
        if verdict is not None and not verdict.is_match:
            reason = verdict.reasoning or "page did not match the entry"
            return None, False, f"rejected: {reason}"
        note = (
            f"confirmed: {verdict.reasoning}"
            if verdict is not None and verdict.reasoning
            else record.alternative_note
        )
        return (
            Alternative(resolved, cached.probability, cached.reasoning),
            verdict is not None,
            note,
        )

    async def _confirm(
        self,
        entry: CatalogEntry,
        url: str,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None,
    ) -> RedirectVerdict | None:
        async with self._gemini:
            return await self.finder.confirm_alternative(
                entry,
                url=url,
                page_title=page_title,
                page_h1=page_h1,
                page_snippet=page_snippet,
            )

    async def _wayback(
        self, url: str, facts: _UrlFacts, verdicts: list[_Verdict]
    ) -> str | None:
        if facts.wayback is not None:
            return facts.wayback
        save = self.save_archive and any(
            verdict.state == "ok" and not verdict.transient for verdict in verdicts
        )
        snapshot = await self.wayback.lookup(url)
        if snapshot is None and save:
            snapshot = await self.wayback.save(url)
            if snapshot is not None:
                self.stats.wayback_captures += 1
        if snapshot is None:
            return None
        self.cache.set_wayback(url, snapshot)
        return snapshot

    async def _alternative_wayback(self, url: str) -> str | None:
        cached = None if self.refresh else self.cache.get_wayback(url)
        if cached is not None:
            return cached
        snapshot = await self.wayback.lookup(url)
        if snapshot is None and self.save_archive:
            snapshot = await self.wayback.save(url)
            if snapshot is not None:
                self.stats.wayback_captures += 1
        if snapshot is not None:
            self.cache.set_wayback(url, snapshot)
        return snapshot

    def _apply(
        self,
        entry: CatalogEntry,
        verdict: _Verdict,
        facts: _UrlFacts,
        wayback_link: str | None,
    ) -> CatalogEntry:
        if verdict.keep:
            return entry

        if verdict.transient:
            if entry.web_link_state is not None:
                return entry
            updates = {
                "web_link_state": verdict.state,
                "web_link_status": verdict.status,
                "web_link_resolved": verdict.resolved_url,
            }
            if wayback_link is not None and entry.wayback_link is None:
                updates["wayback_link"] = wayback_link
            return entry.model_copy(update=updates)

        if verdict.write_entry:
            self.cache.put_entry(
                entry.id,
                entry.web_link or "",
                verdict.entry_record,
                cacheable=not verdict.transient,
            )

        alternative = verdict.alternative
        updates = {
            "web_link_state": verdict.state,
            "web_link_status": verdict.status,
            "web_link_resolved": verdict.resolved_url,
            "alternative_link": alternative.url if alternative else None,
            "alternative_probability": alternative.probability if alternative else None,
            "alternative_reasoning": alternative.reasoning if alternative else None,
            "alternative_note": verdict.note,
        }
        if wayback_link is not None:
            updates["wayback_link"] = wayback_link
        if verdict.fresh:
            updates["verified_at"] = self.cache.now().isoformat()
        return entry.model_copy(update=updates)


def merge_verified(
    catalog: Sequence[CatalogEntry], verified: Sequence[CatalogEntry]
) -> list[CatalogEntry]:
    """Apply verified entries by id, clearing anything tied to an old link."""
    current = {entry.id: entry for entry in catalog}
    updates: dict[str, CatalogEntry] = {}
    for entry in verified:
        if entry.id in updates:
            raise ValueError(f"duplicate verified id: {entry.id}")
        original = current.get(entry.id)
        if original is None:
            raise ValueError(f"verified id not in catalog: {entry.id}")
        if original.web_link != entry.web_link:
            updates[entry.id] = original.model_copy(
                update=dict.fromkeys(VERIFICATION_FIELDS)
            )
        else:
            updates[entry.id] = entry
    return [updates.get(entry.id, entry) for entry in catalog]

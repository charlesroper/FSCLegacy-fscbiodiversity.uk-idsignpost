"""Verification pipeline: HTTP liveness, Gemini verdicts, Wayback, and cache."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from idsignpost.cache import DEFAULT_TTL_DAYS, CatalogCache, EntryRecord
from idsignpost.cost import DEFAULT_COST_FILE, SessionCostTracker
from idsignpost.gemini_finder import (
    DEFAULT_MIN_PROBABILITY,
    DEFAULT_MODEL,
    Alternative,
    Candidate,
    DiscoveryOutcome,
    GeminiFinder,
    Generator,
    RedirectVerdict,
    is_unusable_alternative,
)
from idsignpost.http_checker import (
    DEFAULT_TIMEOUT as DEFAULT_HTTP_TIMEOUT,
    CheckResult,
    HTTPChecker,
)
from idsignpost.models import (
    CatalogEntry,
    LinkState,
    ensure_unique_ids,
    merge_verified as merge_verified,
)
from idsignpost.sampler import get_stratified_sample
from idsignpost.urls import cache_key, is_public_http_url, urls_equal
from idsignpost.wayback import (
    DEFAULT_CONCURRENCY as DEFAULT_WAYBACK_CONCURRENCY,
    WaybackClient,
)

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

    async def discover(
        self, entry: CatalogEntry, *, hint: str | None = None
    ) -> list[Candidate] | None: ...

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
        page_snippet: str | None = None,
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
class VerifyConfig:
    """Typed options for a verification run."""

    command: str = "verify"
    cache_file: str | Path = ".url_cache.json"
    cache_ttl_days: int = DEFAULT_TTL_DAYS
    cost_file: str | Path = DEFAULT_COST_FILE
    sample: int | None = None
    seed: int = 42
    limit: int | None = None
    only_broken: bool = False
    save_archive: bool = False
    refresh: bool = False
    concurrency: int = DEFAULT_CONCURRENCY
    wayback_concurrency: int = DEFAULT_WAYBACK_CONCURRENCY
    gemini_concurrency: int = DEFAULT_GEMINI_CONCURRENCY
    timeout: float = DEFAULT_HTTP_TIMEOUT
    min_prob: float = DEFAULT_MIN_PROBABILITY
    model: str = DEFAULT_MODEL
    verification_model: str | None = None
    discovery_model: str | None = None
    max_searches: int | None = None
    api_key: str | None = None

    @property
    def verification_model_name(self) -> str:
        """Return the model used for redirect and confirmation checks."""
        return self.verification_model or self.model

    @property
    def discovery_model_name(self) -> str:
        """Return the model used for grounded alternative discovery."""
        return self.discovery_model or self.model

    def validate(self) -> str | None:
        """Return a usage error message when any run option is out of range."""
        if self.sample is not None and self.limit is not None:
            return "--sample and --limit are mutually exclusive"
        if self.sample is not None and self.sample < 1:
            return "--sample must be at least 1"
        if self.limit is not None and self.limit < 1:
            return "--limit must be at least 1"
        if not 0.0 <= self.min_prob <= 1.0:
            return "--min-prob must be between 0 and 1"
        for value, label in (
            (self.concurrency, "--concurrency"),
            (self.wayback_concurrency, "--wayback-concurrency"),
            (self.gemini_concurrency, "--gemini-concurrency"),
        ):
            if value < 1:
                return f"{label} must be at least 1"
        if not self.timeout > 0:
            return "--timeout must be positive"
        if self.cache_ttl_days < 0:
            return "--cache-ttl-days must not be negative"
        if self.max_searches is not None and self.max_searches < 0:
            return "--max-searches must not be negative"
        return None

    def select_targets(self, entries: Sequence[CatalogEntry]) -> list[CatalogEntry]:
        """Apply --only-broken, then --sample or --limit."""
        pool = [
            entry
            for entry in entries
            if not self.only_broken or entry.web_link_state == "broken"
        ]
        if self.sample is not None:
            return get_stratified_sample(pool, self.sample, self.seed)
        if self.limit is not None:
            return pool[: self.limit]
        return pool


@dataclass(frozen=True, slots=True)
class VerifyDeps:
    """Optional adapter overrides for verification runs and tests."""

    checker: Checker | None = None
    finder: Finder | None = None
    wayback: Archive | None = None
    generator: Generator | None = None
    resolver: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class _UrlFacts:
    result: CheckResult
    transient: bool
    checked: bool


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


def _alternative_fields(entry: CatalogEntry) -> Alternative | None:
    if not entry.alternative_link:
        return None
    return Alternative(
        url=entry.alternative_link,
        probability=entry.alternative_probability or 0.0,
        reasoning=entry.alternative_reasoning,
    )


def _join_notes(notes: list[str], limit: int = 3) -> str | None:
    """Join the first few candidate notes into one cacheable line."""
    if not notes:
        return None
    return "; ".join(notes[:limit])


def _url_policy_note(url: str, broken_link: str) -> str | None:
    """Apply pure URL policy checks without DNS or network I/O."""
    try:
        if urlsplit(url).scheme.lower() not in {"http", "https"}:
            return "ignored: not an http link"
        if is_unusable_alternative(url):
            return "ignored: blocked host or image file"
        cleaned = cache_key(url)
        if urlsplit(cleaned).path in {"", "/"}:
            return "ignored: site homepage"
        if urls_equal(cleaned, broken_link):
            return "ignored: same as the broken link"
    except ValueError:
        return "ignored: not an http link"
    return None


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
        resolver: Callable[..., Any] | None = None,
    ) -> None:
        self.cache = cache
        self.checker = checker
        self.finder = finder
        self.wayback = wayback
        self.save_archive = save_archive
        self.refresh = refresh
        self._resolver = resolver
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

        interrupted: KeyboardInterrupt | None = None

        async def run_group(members: list[CatalogEntry]) -> list[CatalogEntry]:
            nonlocal interrupted
            try:
                return await self._verify_group(members)
            except KeyboardInterrupt as exc:
                interrupted = exc
                return []

        updated: dict[str, CatalogEntry] = {}
        tasks = [asyncio.create_task(run_group(members)) for members in groups.values()]
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                first_error: BaseException | None = None
                for task in done:
                    error = task.exception()
                    if error is not None and first_error is None:
                        first_error = error
                if interrupted is not None:
                    raise interrupted
                if first_error is not None:
                    raise first_error
                for task in done:
                    for entry in task.result():
                        updated[entry.id] = entry
        except BaseException:
            for other in pending:
                other.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise

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

        note = "dropped: URL shared by three or more entries"
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
                    "alternative_note": note,
                    "wayback_link": orig_wayback,
                }
            )
            if entry.web_link:
                self.cache.drop_alternative(entry.id, entry.web_link, note)
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
            allow_primary_save = self.save_archive and any(
                verdict.state == "ok" and not verdict.transient for verdict in verdicts
            )
            wayback_link = await self._resolve_wayback(
                url, allow_save=allow_primary_save
            )
            wayback_links = [
                wayback_link
                if wayback_link is not None
                or verdict.alternative is None
                or verdict.transient
                else await self._resolve_wayback(
                    verdict.alternative.url, allow_save=self.save_archive
                )
                for verdict in verdicts
            ]
        return [
            self._apply(member, verdict, member_wayback)
            for member, verdict, member_wayback in zip(
                members, verdicts, wayback_links, strict=True
            )
        ]

    async def _facts(self, url: str, title: str) -> _UrlFacts:
        cached = None if self.refresh else self.cache.get_check(url)
        if cached is not None:
            self.stats.cache_hits += 1
            return _UrlFacts(result=cached, transient=False, checked=False)

        result = await self.checker.check(url, entry_title=title)
        self.stats.checked += 1
        if self.checker.should_abort():
            raise OfflineRunError(
                f"every check failed at the network level after {self.stats.checked} attempts"
            )
        self.cache.put_check(url, result)
        return _UrlFacts(
            result=result,
            transient=not result.cacheable,
            checked=True,
        )

    def _policy_note(self, url: str, broken_link: str) -> str | None:
        note = _url_policy_note(url, broken_link)
        if note is not None:
            return note
        cleaned = cache_key(url)
        public = (
            is_public_http_url(cleaned, resolver=self._resolver)
            if self._resolver is not None
            else is_public_http_url(cleaned)
        )
        if not public:
            return "ignored: private address"
        return None

    async def _verdict(self, entry: CatalogEntry, facts: _UrlFacts) -> _Verdict:
        result = facts.result
        url = entry.web_link or ""
        record = (
            None if self.refresh or not url else self.cache.get_entry(entry.id, url)
        )
        previous = _alternative_fields(entry)

        if facts.transient:
            return _Verdict(
                state=entry.web_link_state or result.state,
                status=(
                    entry.web_link_status
                    if entry.web_link_state is not None
                    else result.status
                ),
                resolved_url=(
                    entry.web_link_resolved
                    if entry.web_link_state is not None
                    else result.resolved_url
                ),
                alternative=previous,
                transient=True,
            )

        state: LinkState | None = result.state
        status = result.status
        resolved = result.resolved_url
        redirect_verdict: RedirectVerdict | None = None
        fresh = facts.checked

        if state == "ok" and result.is_suspicious_redirect:
            if record is not None and record.redirect_is_match is not None:
                redirect_verdict = RedirectVerdict(
                    record.redirect_is_match, record.redirect_reason
                )
            else:
                redirect_verdict = await self._verify_redirect(entry, facts, resolved)
                if redirect_verdict is None:
                    return _Verdict(
                        state=entry.web_link_state,
                        status=entry.web_link_status,
                        resolved_url=entry.web_link_resolved,
                        alternative=previous,
                        keep=True,
                    )
                fresh = True
            if not redirect_verdict.is_match:
                state, status = "broken", "redirect_mismatch"

        if state != "broken":
            self._clear_recovered_entry(entry.id, url, redirect_verdict)
            return _Verdict(
                state=state,
                status=status,
                resolved_url=resolved,
                alternative=None,
                fresh=fresh,
            )

        outcome, ran_discovery = await self._resolve_broken_alternative(
            entry, url, record, previous
        )
        if outcome.transient:
            if url:
                self.cache.record_redirect(
                    entry.id,
                    url,
                    is_match=(
                        redirect_verdict.is_match
                        if redirect_verdict is not None
                        else None
                    ),
                    reason=(
                        redirect_verdict.reasoning
                        if redirect_verdict is not None
                        else None
                    ),
                )
            return _Verdict(
                state=state,
                status=status,
                resolved_url=resolved,
                alternative=outcome.alternative,
                note=outcome.note,
                fresh=False,
            )

        if url:
            self.cache.update_entry(
                entry.id,
                url,
                redirect_is_match=(
                    redirect_verdict.is_match if redirect_verdict is not None else None
                ),
                redirect_reason=(
                    redirect_verdict.reasoning if redirect_verdict is not None else None
                ),
                alternative_url=outcome.alternative.url
                if outcome.alternative
                else None,
                alternative_probability=(
                    outcome.alternative.probability if outcome.alternative else None
                ),
                alternative_reasoning=(
                    outcome.alternative.reasoning if outcome.alternative else None
                ),
                alternative_note=outcome.note,
                alternative_checked=True,
                alternative_confirmed=(
                    outcome.confirmed if outcome.alternative is not None else False
                ),
            )
        return _Verdict(
            state=state,
            status=status,
            resolved_url=resolved,
            alternative=outcome.alternative,
            note=outcome.note,
            fresh=fresh or ran_discovery,
        )

    def _clear_recovered_entry(
        self, entry_id: str, url: str, redirect_verdict: RedirectVerdict | None
    ) -> None:
        if not url:
            return
        if redirect_verdict is not None:
            self.cache.update_entry(
                entry_id,
                url,
                redirect_is_match=redirect_verdict.is_match,
                redirect_reason=redirect_verdict.reasoning,
            )
            return
        self.cache.clear_entry(entry_id, url)

    async def _resolve_broken_alternative(
        self,
        entry: CatalogEntry,
        url: str,
        record: EntryRecord | None,
        previous: Alternative | None,
    ) -> tuple[DiscoveryOutcome, bool]:
        if record is not None and record.alternative_checked:
            cached_url = record.alternative_url
            if not cached_url:
                return DiscoveryOutcome(note=record.alternative_note), False
            if record.alternative_confirmed:
                policy_note = _url_policy_note(cached_url, url)
                if policy_note is not None:
                    return DiscoveryOutcome(note=policy_note), False
                return (
                    DiscoveryOutcome(
                        alternative=Alternative(
                            cached_url,
                            record.alternative_probability or 0.0,
                            record.alternative_reasoning,
                        ),
                        confirmed=True,
                        note=record.alternative_note,
                    ),
                    False,
                )
            return await self._reconfirm(entry, record), False

        outcome = await self._discover(entry)
        if outcome.transient:
            return (
                DiscoveryOutcome(
                    alternative=previous,
                    transient=True,
                    confirmed=False,
                    note=entry.alternative_note or outcome.note,
                ),
                False,
            )
        return outcome, True

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

    async def _evaluate_candidate(
        self, entry: CatalogEntry, candidate: Candidate
    ) -> DiscoveryOutcome:
        """Apply policy, cached HTTP liveness, and confirmation to one candidate."""
        broken_link = entry.web_link or ""
        policy_note = self._policy_note(candidate.url, broken_link)
        if policy_note is not None:
            return DiscoveryOutcome(note=policy_note)

        cleaned = cache_key(candidate.url)
        facts = await self._facts(cleaned, entry.title)
        if facts.transient:
            return DiscoveryOutcome(
                transient=True, note="retry: transient check failure"
            )
        result = facts.result
        if result.state != "ok" or result.is_suspicious_redirect:
            return DiscoveryOutcome(
                note=f"rejected: link returned {result.status or result.state}"
            )
        if result.resolved_url:
            redirect_note = self._policy_note(result.resolved_url, broken_link)
            if redirect_note is not None:
                return DiscoveryOutcome(note=redirect_note)

        url = result.resolved_url or cleaned
        verdict = await self._confirm(
            entry,
            url,
            result.page_title,
            result.page_h1,
            result.page_snippet,
        )
        if verdict is not None and not verdict.is_match:
            reason = verdict.reasoning or "page did not match the entry"
            return DiscoveryOutcome(note=f"rejected: {reason}")
        note = (
            f"confirmed: {verdict.reasoning}"
            if verdict is not None and verdict.reasoning
            else None
        )
        return DiscoveryOutcome(
            alternative=Alternative(url, candidate.probability, candidate.reasoning),
            confirmed=verdict is not None,
            note=note,
        )

    async def _discover(self, entry: CatalogEntry) -> DiscoveryOutcome:
        """Try any stored alternative first, then discover and evaluate candidates."""
        if not entry.web_link:
            return DiscoveryOutcome()

        transient = False
        rejected = False
        notes: list[str] = []
        preferred = _alternative_fields(entry)
        if preferred is not None:
            attempt = await self._evaluate_candidate(
                entry,
                Candidate(preferred.url, preferred.probability, preferred.reasoning),
            )
            transient = transient or attempt.transient
            rejected = attempt.alternative is None and not attempt.transient
            if attempt.note:
                notes.append(attempt.note)
            if attempt.alternative is not None:
                return attempt

        async with self._gemini:
            candidates = await self.finder.discover(entry)
        if candidates is None:
            return DiscoveryOutcome(
                transient=not rejected, note=_join_notes(notes) or "search failed"
            )

        for candidate in candidates:
            attempt = await self._evaluate_candidate(entry, candidate)
            transient = transient or attempt.transient
            if attempt.note:
                notes.append(attempt.note)
            if attempt.alternative is not None:
                return attempt
        return DiscoveryOutcome(
            transient=transient,
            note=_join_notes(notes) or "no candidates returned",
        )

    async def _reconfirm(
        self, entry: CatalogEntry, record: EntryRecord
    ) -> DiscoveryOutcome:
        """Recheck a cached alternative that was stored before confirmation."""
        url = record.alternative_url
        if not url:
            return DiscoveryOutcome(note=record.alternative_note)
        candidate = Candidate(
            url,
            record.alternative_probability or 0.0,
            record.alternative_reasoning,
        )
        outcome = await self._evaluate_candidate(entry, candidate)
        if outcome.transient:
            return DiscoveryOutcome(
                alternative=Alternative(
                    url, candidate.probability, candidate.reasoning
                ),
                transient=True,
                confirmed=False,
                note=record.alternative_note or entry.alternative_note or outcome.note,
            )
        if outcome.alternative is not None and outcome.note is None:
            return DiscoveryOutcome(
                alternative=outcome.alternative,
                confirmed=outcome.confirmed,
                note=record.alternative_note,
            )
        return outcome

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

    async def _resolve_wayback(self, url: str, *, allow_save: bool) -> str | None:
        """Return a cached or newly resolved Wayback snapshot for a URL."""
        existing = self.cache.get_wayback(url)
        if not self.refresh and existing is not None:
            return existing
        snapshot = await self.wayback.lookup(url)
        if snapshot is None and existing is None and allow_save:
            snapshot = await self.wayback.save(url)
            if snapshot is not None:
                self.stats.wayback_captures += 1
        if snapshot is not None:
            self.cache.set_wayback(url, snapshot)
            return snapshot
        return existing

    def _apply(
        self,
        entry: CatalogEntry,
        verdict: _Verdict,
        wayback_link: str | None,
    ) -> CatalogEntry:
        if verdict.keep:
            return entry

        if verdict.transient:
            if entry.web_link_state is not None:
                return entry
            updates: dict[str, Any] = {
                "web_link_state": verdict.state,
                "web_link_status": verdict.status,
                "web_link_resolved": verdict.resolved_url,
            }
            if wayback_link is not None and entry.wayback_link is None:
                updates["wayback_link"] = wayback_link
            return entry.model_copy(update=updates)

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


async def _close_adapter(adapter: object) -> None:
    aclose = getattr(adapter, "aclose", None)
    if callable(aclose):
        result = aclose()
        if inspect.isawaitable(result):
            await result


async def run_verification(
    entries: Sequence[CatalogEntry],
    config: VerifyConfig,
    *,
    deps: VerifyDeps | None = None,
) -> tuple[list[CatalogEntry], VerifyStats]:
    """Validate options, run verification over selected targets, and merge results."""
    error = config.validate()
    if error is not None:
        raise ValueError(error)
    ensure_unique_ids(entries)

    active_deps = deps or VerifyDeps()
    targets = config.select_targets(entries)
    tracker = SessionCostTracker.load(config.cost_file)
    cache = CatalogCache.load(config.cache_file, ttl_days=config.cache_ttl_days)

    active_checker: Checker = (
        active_deps.checker
        if active_deps.checker is not None
        else HTTPChecker(timeout=config.timeout)
    )
    active_wayback: Archive = (
        active_deps.wayback
        if active_deps.wayback is not None
        else WaybackClient(
            timeout=config.timeout, concurrency=config.wayback_concurrency
        )
    )
    try:
        active_finder: Finder = (
            active_deps.finder
            if active_deps.finder is not None
            else GeminiFinder(
                verification_model=config.verification_model_name,
                discovery_model=config.discovery_model_name,
                api_key=config.api_key,
                generator=active_deps.generator,
                min_probability=config.min_prob,
                max_searches=config.max_searches,
                tracker=tracker,
            )
        )
        verifier = Verifier(
            cache=cache,
            checker=active_checker,
            finder=active_finder,
            wayback=active_wayback,
            concurrency=config.concurrency,
            gemini_concurrency=config.gemini_concurrency,
            save_archive=config.save_archive,
            refresh=config.refresh,
            resolver=active_deps.resolver,
        )
        verified = await verifier.verify(targets)
    except (KeyboardInterrupt, asyncio.CancelledError):
        cache.save()
        raise
    finally:
        try:
            await _close_adapter(active_checker)
        finally:
            await _close_adapter(active_wayback)

    cache.save()
    run_record = tracker.finish_run(command=config.command)
    verifier.stats.cost_summary = tracker.format_run_summary(run_record)
    return merge_verified(entries, verified), verifier.stats

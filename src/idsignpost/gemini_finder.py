"""Gemini-backed redirect verification and grounded alternative discovery."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from idsignpost.http_checker import CheckResult
from idsignpost.models import CatalogEntry
from idsignpost.urls import cache_key, is_public_http_url, urls_equal

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MIN_PROBABILITY = 0.40
MAX_ATTEMPTS = 4
BACKOFF = (1.0, 2.0, 4.0)
IMAGE_URL = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|bmp|tiff?)(?:$|[?&#])", re.IGNORECASE
)

BLOCKED_HOSTS = (
    "fscbiodiversity.uk",
    "tombio.uk",
    "archive.org",
    "web.archive.org",
    "ebay.co.uk",
    "ebay.com",
    "zvab.com",
    "scribd.com",
)


@dataclass(frozen=True, slots=True)
class Candidate:
    url: str
    probability: float
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class RedirectVerdict:
    is_match: bool
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class Alternative:
    url: str
    probability: float
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """Result of an alternative search, flagging transient failures."""

    alternative: Alternative | None = None
    transient: bool = False
    confirmed: bool = False
    note: str | None = None


@dataclass(frozen=True, slots=True)
class _Attempt:
    """Outcome of checking one candidate URL."""

    alternative: Alternative | None = None
    transient: bool = False
    confirmed: bool = False
    note: str | None = None


class Checker(Protocol):
    async def check(
        self, url: str, *, entry_title: str | None = None
    ) -> CheckResult: ...


def is_blocked_host(url: str) -> bool:
    """Match blocked hosts by exact host or dot-suffix, never substring."""
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS
    )


def is_unusable_alternative(url: str) -> bool:
    """Reject blocked hosts and direct image files as replacement pages."""
    return is_blocked_host(url) or bool(IMAGE_URL.search(url))


def _join_notes(notes: list[str], limit: int = 3) -> str | None:
    """Join the first few candidate notes into one cacheable line."""
    if not notes:
        return None
    return "; ".join(notes[:limit])


def _load_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    for candidate in (cleaned, _slice_json(cleaned)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _slice_json(text: str) -> str:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return ""
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else ""


def parse_candidates(text: str) -> list[Candidate] | None:
    """Parse ranked candidates, returning None only for malformed JSON."""
    payload = _load_json(text)
    if payload is None:
        return None
    raw = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return None

    candidates: list[Candidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        probability = item.get("probability")
        if not isinstance(url, str) or not url.strip():
            continue
        if not isinstance(probability, (int, float)) or isinstance(probability, bool):
            continue
        score = float(probability)
        if not 0.0 <= score <= 1.0:
            continue
        reasoning = item.get("reasoning")
        candidates.append(
            Candidate(
                url=url.strip(),
                probability=score,
                reasoning=reasoning if isinstance(reasoning, str) else None,
            )
        )
    return candidates


def parse_redirect_verdict(text: str) -> RedirectVerdict | None:
    payload = _load_json(text)
    if not isinstance(payload, dict):
        return None
    match = payload.get("is_match")
    if isinstance(match, str):
        match = match.strip().lower() in {"true", "yes", "1"}
    if not isinstance(match, bool):
        return None
    reasoning = payload.get("reasoning")
    return RedirectVerdict(
        is_match=match, reasoning=reasoning if isinstance(reasoning, str) else None
    )


def _retryable(exc: BaseException) -> bool:
    for attribute in ("code", "status_code"):
        code = getattr(exc, attribute, None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code == 429 or 500 <= code < 600
    message = str(exc)
    return any(token in message for token in ("429", "500", "502", "503", "504"))


class GeminiFinder:
    """Ask Gemini whether a redirect still fits, and where a guide moved."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_probability: float = DEFAULT_MIN_PROBABILITY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        resolver: Callable[..., Any] | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("GEMINI_API_KEY is required for Gemini lookups")
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client
        self.model = model
        self.timeout = timeout
        self.min_probability = min_probability
        self._sleep = sleep
        self._resolver = resolver

    async def _generate(self, prompt: str, *, json_mode: bool) -> str | None:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self.model,
                        contents=prompt,
                        config=self._config(json_mode),
                    ),
                    timeout=self.timeout,
                )
                return response.text or ""
            except Exception as exc:
                if not _retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    return None
                await self._sleep(BACKOFF[attempt])
        return None

    @staticmethod
    def _config(json_mode: bool) -> Any:
        from google.genai import types

        options: dict[str, Any] = {
            "temperature": 0.2,
            "tools": [types.Tool(google_search=types.GoogleSearch())],
        }
        if json_mode:
            options["response_mime_type"] = "application/json"
        return types.GenerateContentConfig(**options)

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
    ) -> RedirectVerdict | None:
        """Decide whether a suspicious redirect still hosts the guide."""
        prompt = (
            "A catalogue entry lost its original link to a redirect. Decide whether "
            "the final page still offers the same guide.\n"
            f"Entry title: {title}\n"
            f"Authors: {authors or 'unknown'}\n"
            f"Description: {description or 'unknown'}\n"
            f"Original link: {web_link}\n"
            f"Final link: {resolved_url or web_link}\n"
            f"Final page title: {page_title or 'unknown'}\n"
            f"Final page heading: {page_h1 or 'unknown'}\n"
            'Reply as JSON: {"is_match": true or false, "reasoning": "one sentence"}.'
        )
        text = await self._generate(prompt, json_mode=True)
        return parse_redirect_verdict(text) if text is not None else None

    async def confirm_alternative(
        self,
        entry: CatalogEntry,
        *,
        url: str,
        page_title: str | None,
        page_h1: str | None,
    ) -> RedirectVerdict | None:
        """Decide whether a live candidate offers this guide, not something else."""
        prompt = (
            "A catalogue entry lost its link and a candidate replacement was found. "
            "Decide whether the candidate page offers the same guide. Reject a "
            "different publication, a category or index page, and an error page.\n"
            f"Entry title: {entry.title}\n"
            f"Authors: {entry.authors or 'unknown'}\n"
            f"Description: {entry.description or 'unknown'}\n"
            f"Candidate link: {url}\n"
            f"Page title: {page_title or 'unknown'}\n"
            f"Page heading: {page_h1 or 'unknown'}\n"
            'Reply as JSON: {"is_match": true or false, "reasoning": "one sentence"}.'
        )
        text = await self._generate(prompt, json_mode=True)
        return parse_redirect_verdict(text) if text is not None else None

    async def discover(self, entry: CatalogEntry) -> list[Candidate] | None:
        """Ask for up to three ranked replacement URLs for a broken link."""
        if not entry.web_link:
            return None
        prompt = (
            "Find the exact page that hosts or offers this identification guide. "
            "Return at most three candidate URLs, most likely first. "
            "Do not return a site homepage, search page, or general category page.\n"
            f"Title: {entry.title}\n"
            f"Authors: {entry.authors or 'unknown'}\n"
            f"Description: {entry.description or 'unknown'}\n"
            f"Available as: {', '.join(entry.available_as) or 'unknown'}\n"
            f"Keywords: {entry.keywords or 'unknown'}\n"
            f"Broken link: {entry.web_link}\n"
            'Reply as JSON: {"candidates": [{"url": "...", "probability": 0.0, '
            '"reasoning": "one sentence"}]}. '
            "Use a probability between 0 and 1 for each candidate."
        )
        for attempt in range(2):
            text = await self._generate(prompt, json_mode=True)
            if text is None:
                return None
            candidates = parse_candidates(text)
            if candidates is not None:
                return candidates
            if attempt == 0:
                continue
        return None

    async def find_alternative(
        self,
        entry: CatalogEntry,
        *,
        checker: Checker,
        preferred: Alternative | None = None,
    ) -> DiscoveryOutcome:
        """Return the first verified candidate, trying a known one first."""
        if not entry.web_link:
            return DiscoveryOutcome()

        transient = False
        rejected = False
        notes: list[str] = []
        if preferred is not None:
            candidate = Candidate(
                preferred.url, preferred.probability, preferred.reasoning
            )
            attempt = await self._try_candidate(entry, candidate, checker)
            transient = transient or attempt.transient
            rejected = attempt.alternative is None and not attempt.transient
            if attempt.note:
                notes.append(attempt.note)
            if attempt.alternative is not None:
                return DiscoveryOutcome(
                    alternative=attempt.alternative,
                    confirmed=attempt.confirmed,
                    note=attempt.note,
                )

        candidates = await self.discover(entry)
        if candidates is None:
            return DiscoveryOutcome(
                transient=not rejected, note=_join_notes(notes) or "search failed"
            )

        for candidate in candidates:
            if candidate.probability < self.min_probability:
                continue
            attempt = await self._try_candidate(entry, candidate, checker)
            transient = transient or attempt.transient
            if attempt.note:
                notes.append(attempt.note)
            if attempt.alternative is not None:
                return DiscoveryOutcome(
                    alternative=attempt.alternative,
                    confirmed=attempt.confirmed,
                    note=attempt.note,
                )
        return DiscoveryOutcome(
            transient=transient,
            note=_join_notes(notes) or "no candidates returned",
        )

    async def _try_candidate(
        self, entry: CatalogEntry, candidate: Candidate, checker: Checker
    ) -> _Attempt:
        """Apply policy, liveness, and confirmation to one candidate."""
        if urlsplit(candidate.url).scheme.lower() not in {"http", "https"}:
            return _Attempt(note="ignored: not an http link")
        if is_unusable_alternative(candidate.url):
            return _Attempt(note="ignored: blocked host or image file")
        cleaned = cache_key(candidate.url)
        if urlsplit(cleaned).path in {"", "/"}:
            return _Attempt(note="ignored: site homepage")
        if urls_equal(cleaned, entry.web_link or ""):
            return _Attempt(note="ignored: same as the broken link")
        if not is_public_http_url(cleaned, **self._resolver_kwargs()):
            return _Attempt(note="ignored: private address")
        result = await checker.check(cleaned, entry_title=entry.title)
        if not result.cacheable:
            return _Attempt(transient=True, note="retry: transient check failure")
        if result.state != "ok" or result.is_suspicious_redirect:
            return _Attempt(
                note=f"rejected: link returned {result.status or result.state}"
            )
        url = result.resolved_url or cleaned
        verdict = await self.confirm_alternative(
            entry, url=url, page_title=result.page_title, page_h1=result.page_h1
        )
        if verdict is not None and not verdict.is_match:
            reason = verdict.reasoning or "page did not match the entry"
            return _Attempt(note=f"rejected: {reason}")
        note = (
            f"confirmed: {verdict.reasoning}"
            if verdict is not None and verdict.reasoning
            else None
        )
        return _Attempt(
            alternative=Alternative(url, candidate.probability, candidate.reasoning),
            confirmed=verdict is not None,
            note=note,
        )

    def _resolver_kwargs(self) -> dict[str, Any]:
        return {"resolver": self._resolver} if self._resolver is not None else {}

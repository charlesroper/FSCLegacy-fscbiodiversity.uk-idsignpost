"""Tests for Gemini redirect verdicts and alternative discovery."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from idsignpost.gemini_finder import (
    Alternative,
    Candidate,
    DiscoveryOutcome,
    GeminiFinder,
    RedirectVerdict,
    is_blocked_host,
    parse_candidates,
    parse_redirect_verdict,
)
from idsignpost.http_checker import CheckResult
from idsignpost.models import CatalogEntry

ENTRY_DATA: dict[str, Any] = {
    "id": "bats-flight",
    "filename": "bats-flight.html",
    "title": "Bats in flight",
    "authors": "Natural History Museum",
    "description": "Identify five common bat species.",
    "available_as": ["PDF"],
    "keywords": "bats",
    "web_link": "https://example.org/old-guide",
}
BROKEN_URL = ENTRY_DATA["web_link"]


class FakeResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


class FakeAio:
    def __init__(self, models: Any) -> None:
        self.models = models


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = FakeModels(script)
        self.aio = FakeAio(self.models)


class FakeChecker:
    def __init__(self, results: dict[str, CheckResult]) -> None:
        self.results = results
        self.checked: list[str] = []

    async def check(self, url: str, *, entry_title: str | None = None) -> CheckResult:
        self.checked.append(url)
        return self.results.get(url, CheckResult("404", "broken"))


def public_resolver(host: str, port: Any) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def make_finder(
    script: list[Any],
    *,
    min_probability: float = 0.4,
    sleep: Any = None,
) -> tuple[GeminiFinder, FakeClient, list[float]]:
    client = FakeClient(script)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    finder = GeminiFinder(
        client=client,
        min_probability=min_probability,
        sleep=sleep or fake_sleep,
        resolver=public_resolver,
    )
    return finder, client, sleeps


def entry() -> CatalogEntry:
    return CatalogEntry.model_validate(ENTRY_DATA)


def live(url: str) -> CheckResult:
    return CheckResult("200", "ok", resolved_url=None)


def test_parse_candidates_accepts_fenced_json() -> None:
    text = '```json\n{"candidates": [{"url": "https://a.example", "probability": 0.8}]}\n```'

    candidates = parse_candidates(text)

    assert candidates == [Candidate("https://a.example", 0.8)]


def test_parse_candidates_rejects_malformed_json() -> None:
    assert parse_candidates("not json") is None


def test_parse_candidates_keeps_valid_entries_only() -> None:
    text = (
        '{"candidates": ['
        '{"url": "https://a.example", "probability": 0.8, "reasoning": "same"},'
        '{"url": "", "probability": 0.9},'
        '{"url": "https://b.example", "probability": 1.5},'
        '{"url": "https://c.example", "probability": true}'
        "]}"
    )

    assert parse_candidates(text) == [Candidate("https://a.example", 0.8, "same")]


def test_parse_candidates_returns_empty_for_valid_empty_list() -> None:
    assert parse_candidates('{"candidates": []}') == []


def test_parse_redirect_verdict_accepts_boolean_and_string() -> None:
    assert parse_redirect_verdict('{"is_match": true, "reasoning": "yes"}') == (
        RedirectVerdict(True, "yes")
    )
    assert parse_redirect_verdict('{"is_match": "false"}') == RedirectVerdict(False)


def test_parse_redirect_verdict_rejects_missing_flag() -> None:
    assert parse_redirect_verdict('{"reasoning": "unclear"}') is None
    assert parse_redirect_verdict("not json") is None


async def test_verify_redirect_returns_verdict() -> None:
    finder, client, _ = make_finder(
        ['{"is_match": false, "reasoning": "Generic homepage."}']
    )

    verdict = await finder.verify_redirect(
        title="Bats in flight",
        authors=None,
        description=None,
        web_link=BROKEN_URL,
        resolved_url="https://example.org/",
        page_title="Welcome",
        page_h1=None,
    )

    assert verdict == RedirectVerdict(False, "Generic homepage.")
    assert client.models.calls[0]["model"] == "gemini-3.1-flash-lite"
    assert "Bats in flight" in client.models.calls[0]["contents"]


async def test_verify_redirect_returns_none_on_transient_failure() -> None:
    finder, _, _ = make_finder([ValueError("boom")])

    verdict = await finder.verify_redirect(
        title="Bats in flight",
        authors=None,
        description=None,
        web_link=BROKEN_URL,
        resolved_url=None,
        page_title=None,
        page_h1=None,
    )

    assert verdict is None


async def test_generate_retries_on_429_with_backoff() -> None:
    error = RuntimeError("429 rate limited")
    finder, client, sleeps = make_finder([error, '{"candidates": []}'])

    candidates = await finder.discover(entry())

    assert candidates == []
    assert len(client.models.calls) == 2
    assert sleeps == [1.0]


async def test_generate_gives_up_after_four_attempts() -> None:
    error = RuntimeError("503 unavailable")
    finder, client, sleeps = make_finder([error] * 5)

    assert await finder.discover(entry()) is None
    assert len(client.models.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_discover_retries_once_on_malformed_json() -> None:
    finder, client, _ = make_finder(
        [
            "not json",
            '{"candidates": [{"url": "https://a.example", "probability": 0.7}]}',
        ]
    )

    candidates = await finder.discover(entry())

    assert candidates == [Candidate("https://a.example", 0.7)]
    assert len(client.models.calls) == 2


async def test_discover_returns_none_after_repeated_malformed_json() -> None:
    finder, _, _ = make_finder(["not json", "still not json"])

    assert await finder.discover(entry()) is None


async def test_discover_prompt_asks_for_the_exact_page() -> None:
    finder, client, _ = make_finder(['{"candidates": []}'])

    await finder.discover(entry())

    contents = client.models.calls[0]["contents"]
    assert "exact page" in contents
    assert "homepage" in contents


@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("https://ebay.co.uk/itm/1", True),
        ("https://www.ebay.co.uk/itm/1", True),
        ("https://notebay.co.uk/guide", False),
        ("https://web.archive.org/web/2020/https://x", True),
        ("https://archive.org/details/x", True),
        ("https://tombio.uk/idsignpost/", True),
        ("https://zvab.com/books", True),
        ("https://www.scribd.com/document/123", True),
        ("https://fscbiodiversity.uk/idsignpost/", True),
    ],
)
def test_blocklist_uses_host_suffix_not_substring(url: str, blocked: bool) -> None:
    assert is_blocked_host(url) is blocked


async def test_find_alternative_falls_back_when_first_candidate_is_dead() -> None:
    finder, _, _ = make_finder(
        [
            '{"candidates": ['
            '{"url": "https://dead.example/guide", "probability": 0.9},'
            '{"url": "https://live.example/guide", "probability": 0.8}'
            "]}",
            '{"is_match": true, "reasoning": "same guide"}',
        ]
    )
    checker = FakeChecker(
        {"https://live.example/guide": live("https://live.example/guide")}
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome == DiscoveryOutcome(
        Alternative("https://live.example/guide", 0.8),
        confirmed=True,
        note="confirmed: same guide",
    )
    assert checker.checked == [
        "https://dead.example/guide",
        "https://live.example/guide",
    ]


async def test_find_alternative_skips_candidates_below_threshold() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "https://weak.example/guide", "probability": 0.2}]}']
    )
    checker = FakeChecker(
        {"https://weak.example/guide": live("https://weak.example/guide")}
    )

    assert (await finder.find_alternative(entry(), checker=checker)).alternative is None
    assert checker.checked == []


async def test_find_alternative_rejects_private_addresses_without_checking() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "http://127.0.0.1/guide", "probability": 0.9}]}']
    )
    checker = FakeChecker({"http://127.0.0.1/guide": live("http://127.0.0.1/guide")})

    assert (await finder.find_alternative(entry(), checker=checker)).alternative is None
    assert checker.checked == []


async def test_find_alternative_rejects_site_homepages() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "https://example.org/", "probability": 0.9}]}']
    )
    checker = FakeChecker({"https://example.org/": live("https://example.org/")})

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative is None
    assert checker.checked == []


async def test_find_alternative_rejects_a_page_for_a_different_book() -> None:
    finder, _, _ = make_finder(
        [
            '{"candidates": ['
            '{"url": "https://shop.example/wrong-book", "probability": 0.9},'
            '{"url": "https://shop.example/right-book", "probability": 0.8}'
            "]}",
            '{"is_match": false, "reasoning": "different title"}',
            '{"is_match": true, "reasoning": "same guide"}',
        ]
    )
    checker = FakeChecker(
        {
            "https://shop.example/wrong-book": live("https://shop.example/wrong-book"),
            "https://shop.example/right-book": live("https://shop.example/right-book"),
        }
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative == Alternative("https://shop.example/right-book", 0.8)


async def test_find_alternative_tries_the_preferred_url_first() -> None:
    finder, client, _ = make_finder(['{"is_match": true, "reasoning": "same guide"}'])
    checker = FakeChecker(
        {"https://known.example/guide": live("https://known.example/guide")}
    )

    outcome = await finder.find_alternative(
        entry(),
        checker=checker,
        preferred=Alternative("https://known.example/guide", 0.7, "from the cache"),
    )

    assert outcome == DiscoveryOutcome(
        Alternative("https://known.example/guide", 0.7, "from the cache"),
        confirmed=True,
        note="confirmed: same guide",
    )
    assert len(client.models.calls) == 1


async def test_find_alternative_falls_back_when_preferred_is_rejected() -> None:
    finder, _, _ = make_finder(
        [
            '{"is_match": false, "reasoning": "different page"}',
            '{"candidates": [{"url": "https://new.example/guide", "probability": 0.8}]}',
            '{"is_match": true, "reasoning": "same guide"}',
        ]
    )
    checker = FakeChecker(
        {
            "https://old.example/guide": live("https://old.example/guide"),
            "https://new.example/guide": live("https://new.example/guide"),
        }
    )

    outcome = await finder.find_alternative(
        entry(),
        checker=checker,
        preferred=Alternative("https://old.example/guide", 0.9),
    )

    assert outcome.alternative == Alternative("https://new.example/guide", 0.8)


async def test_find_alternative_keeps_a_live_candidate_when_confirmation_fails() -> (
    None
):
    finder, _, _ = make_finder(
        [
            '{"candidates": [{"url": "https://shop.example/guide", "probability": 0.9}]}',
            "not json",
        ]
    )
    checker = FakeChecker(
        {"https://shop.example/guide": live("https://shop.example/guide")}
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative == Alternative("https://shop.example/guide", 0.9)


async def test_confirmation_prompt_includes_the_page_metadata() -> None:
    finder, client, _ = make_finder(['{"is_match": true, "reasoning": "same"}'])

    await finder.confirm_alternative(
        entry(),
        url="https://shop.example/guide",
        page_title="Bats in flight",
        page_h1="Bat guide",
    )

    contents = client.models.calls[0]["contents"]
    assert "https://shop.example/guide" in contents
    assert "Bats in flight" in contents
    assert "Bat guide" in contents


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/guide.jpg",
        "https://example.org/image.aspx?File=guide.jpg&Size=400",
        "https://example.org/scan.PNG",
    ],
)
async def test_find_alternative_rejects_image_urls(url: str) -> None:
    finder, _, _ = make_finder(
        [f'{{"candidates": [{{"url": "{url}", "probability": 0.9}}]}}']
    )
    checker = FakeChecker({url: live(url)})

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative is None
    assert checker.checked == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.org/guide",
        BROKEN_URL,
        "ftp://example.org/guide",
    ],
)
async def test_find_alternative_rejects_unsafe_or_identical_urls(url: str) -> None:
    finder, _, _ = make_finder(
        [f'{{"candidates": [{{"url": "{url}", "probability": 0.9}}]}}']
    )
    checker = FakeChecker({url: live(url)})

    assert (await finder.find_alternative(entry(), checker=checker)).alternative is None
    assert checker.checked == []


async def test_find_alternative_strips_tracking_and_uses_resolved_url() -> None:
    finder, _, _ = make_finder(
        [
            '{"candidates": [{"url": "https://example.org/new?utm_source=gemini#top", '
            '"probability": 0.9, "reasoning": "Official page."}]}'
        ]
    )
    checker = FakeChecker(
        {
            "https://example.org/new": CheckResult(
                "200", "ok", resolved_url="https://example.org/final"
            )
        }
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome == DiscoveryOutcome(
        Alternative("https://example.org/final", 0.9, "Official page.")
    )
    assert checker.checked == ["https://example.org/new"]


async def test_find_alternative_rejects_suspicious_redirects() -> None:
    finder, _, _ = make_finder(
        [
            '{"candidates": ['
            '{"url": "https://example.org/hub", "probability": 0.9},'
            '{"url": "https://example.org/real", "probability": 0.6}'
            "]}",
            '{"is_match": true, "reasoning": "same guide"}',
        ]
    )
    checker = FakeChecker(
        {
            "https://example.org/hub": CheckResult(
                "200",
                "ok",
                resolved_url="https://example.org/shop",
                is_suspicious_redirect=True,
            ),
            "https://example.org/real": live("https://example.org/real"),
        }
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome == DiscoveryOutcome(
        Alternative("https://example.org/real", 0.6),
        confirmed=True,
        note="confirmed: same guide",
    )


async def test_find_alternative_requires_web_link() -> None:
    finder, _, _ = make_finder([])
    checker = FakeChecker({})

    outcome = await finder.find_alternative(
        entry().model_copy(update={"web_link": None}), checker=checker
    )

    assert outcome.alternative is None
    assert outcome.transient is False


def test_missing_api_key_fails_fast() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiFinder(api_key=None)


async def test_find_alternative_flags_transient_gemini_failure() -> None:
    finder, _, _ = make_finder([RuntimeError("503 unavailable")] * 4)

    outcome = await finder.find_alternative(entry(), checker=FakeChecker({}))

    assert outcome.alternative is None
    assert outcome.transient is True


async def test_find_alternative_flags_transient_candidate_checks() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "https://slow.example/guide", "probability": 0.9}]}']
    )

    class TransientChecker:
        async def check(
            self, url: str, *, entry_title: str | None = None
        ) -> CheckResult:
            return CheckResult("503", "blocked", cacheable=False)

    outcome = await finder.find_alternative(entry(), checker=TransientChecker())

    assert outcome.alternative is None
    assert outcome.transient is True


async def test_a_rejected_preferred_url_is_not_hidden_by_a_failed_search() -> None:
    finder, _, _ = make_finder(
        [
            '{"is_match": false, "reasoning": "category index"}',
            "not json",
            "not json",
        ]
    )
    checker = FakeChecker(
        {"https://hub.example/list": live("https://hub.example/list")}
    )

    outcome = await finder.find_alternative(
        entry(),
        checker=checker,
        preferred=Alternative("https://hub.example/list", 0.9),
    )

    assert outcome.alternative is None
    assert outcome.transient is False
    assert outcome.note == "rejected: category index"


async def test_confirmation_prompt_includes_page_text() -> None:
    finder, client, _ = make_finder(['{"is_match": true, "reasoning": "same"}'])

    await finder.confirm_alternative(
        entry(),
        url="https://shop.example/guide",
        page_title="World Register of Marine Species",
        page_h1=None,
        page_snippet="Renk, H. (1984). Calorific values of plankton.",
    )

    contents = client.models.calls[0]["contents"]
    assert "Calorific values of plankton" in contents


async def test_discover_hint_names_the_rejected_index() -> None:
    finder, client, _ = make_finder(['{"candidates": []}'])

    await finder.discover(entry(), hint="https://example.org/keys/")

    contents = client.models.calls[0]["contents"]
    assert "https://example.org/keys/ is an index" in contents


async def test_find_alternative_rejects_redirect_to_private_address() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "https://example.org/move", "probability": 0.9}]}']
    )
    checker = FakeChecker(
        {
            "https://example.org/move": CheckResult(
                "200", "ok", resolved_url="http://127.0.0.1/internal"
            )
        }
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative is None
    assert outcome.note == "ignored: private address"


async def test_find_alternative_rejects_redirect_to_blocked_host() -> None:
    finder, _, _ = make_finder(
        ['{"candidates": [{"url": "https://example.org/move", "probability": 0.9}]}']
    )
    checker = FakeChecker(
        {
            "https://example.org/move": CheckResult(
                "200", "ok", resolved_url="https://www.scribd.com/document/123"
            )
        }
    )

    outcome = await finder.find_alternative(entry(), checker=checker)

    assert outcome.alternative is None
    assert outcome.note == "ignored: blocked host or image file"


async def test_distinct_models_and_search_tool_scoping() -> None:
    client = FakeClient(
        [
            '{"is_match": true, "reasoning": "matched"}',
            '{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}',
            '{"is_match": true, "reasoning": "confirmed"}',
        ]
    )
    finder = GeminiFinder(
        client=client,
        verification_model="gemini-3.1-flash-lite",
        discovery_model="gemini-3.8-flash",
        resolver=public_resolver,
    )

    # 1. verify_redirect: should use verification_model and have no search tools
    await finder.verify_redirect(
        title="Bats in flight",
        authors="NHM",
        description="Bat guide",
        web_link="https://example.org/old",
        resolved_url="https://example.org/new",
        page_title="Bats in flight",
        page_h1="Bats in flight",
        page_snippet="Comprehensive guide to bats in flight.",
    )
    call1 = client.models.calls[0]
    assert call1["model"] == "gemini-3.1-flash-lite"
    assert getattr(call1["config"], "tools", None) is None
    assert "Comprehensive guide to bats in flight." in call1["contents"]

    # 2. discover: should use discovery_model and attach Google Search tool
    await finder.discover(entry())
    call2 = client.models.calls[1]
    assert call2["model"] == "gemini-3.8-flash"
    assert call2["config"].tools is not None
    assert len(call2["config"].tools) == 1

    # 3. confirm_alternative: should use verification_model and have no search tools
    await finder.confirm_alternative(
        entry(),
        url="https://example.org/found",
        page_title="Bats in flight",
        page_h1="Bats in flight",
    )
    call3 = client.models.calls[2]
    assert call3["model"] == "gemini-3.1-flash-lite"
    assert getattr(call3["config"], "tools", None) is None


async def test_max_searches_ceiling_stops_discovery() -> None:
    class FakeGroundingMetadata:
        web_search_queries = ["query 1", "query 2"]

    class FakeCandidate:
        grounding_metadata = FakeGroundingMetadata()

    class SearchResponse:
        text = (
            '{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}'
        )
        usage_metadata = None
        candidates = [FakeCandidate()]

    class SearchModels:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def generate_content(self, **kwargs: Any) -> SearchResponse:
            self.calls.append(kwargs)
            return SearchResponse()

    class SearchClient:
        def __init__(self) -> None:
            self.models = SearchModels()
            self.aio = FakeAio(self.models)

    client = SearchClient()
    finder = GeminiFinder(
        client=client,
        max_searches=2,
        resolver=public_resolver,
    )

    # First discover executes 2 searches, hitting the limit
    candidates1 = await finder.discover(entry())
    assert candidates1 is not None
    assert finder.total_searches == 2

    # Second discover aborts early because max_searches is reached
    candidates2 = await finder.discover(entry())
    assert candidates2 is None
    assert len(client.models.calls) == 1


async def test_cost_tracker_captures_gemini_usage(tmp_path: Path) -> None:
    from idsignpost.cost import SessionCostTracker

    class FakeUsage:
        prompt_token_count = 120
        candidates_token_count = 30
        cached_content_token_count = 20

    class UsageResponse:
        text = '{"is_match": true, "reasoning": "matched"}'
        usage_metadata = FakeUsage()
        candidates = None

    class UsageModels:
        async def generate_content(self, **kwargs: Any) -> UsageResponse:
            return UsageResponse()

    class UsageClient:
        def __init__(self) -> None:
            self.aio = FakeAio(UsageModels())

    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    finder = GeminiFinder(
        client=UsageClient(),
        tracker=tracker,
        resolver=public_resolver,
    )

    await finder.verify_redirect(
        title="Bats",
        authors="NHM",
        description="Bats",
        web_link="https://example.org/old",
        resolved_url="https://example.org/new",
        page_title="Bats",
        page_h1="Bats",
    )

    run = tracker.finish_run(command="test")
    assert run.calls == 1
    assert run.prompt_tokens == 120
    assert run.output_tokens == 30
    assert run.cached_tokens == 20
    assert "redirect_verification" in run.by_operation

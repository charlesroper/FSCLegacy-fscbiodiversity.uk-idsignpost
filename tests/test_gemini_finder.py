"""Tests for Gemini redirect verdicts and alternative discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from idsignpost.gemini_finder import (
    DISCOVERY_PROMPT,
    Candidate,
    GeminiFinder,
    GenerationResult,
    RedirectVerdict,
    is_blocked_host,
    parse_candidates,
    parse_redirect_verdict,
)
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


@dataclass(frozen=True, slots=True)
class RecordedCall:
    prompt: str
    model: str
    json_mode: bool
    search: bool
    thinking_budget: int | None = None


class FakeGenerator:
    def __init__(self, script: list[str | GenerationResult | BaseException]) -> None:
        self.script = list(script)
        self.calls: list[RecordedCall] = []

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        json_mode: bool,
        search: bool,
        thinking_budget: int | None = None,
    ) -> GenerationResult:
        self.calls.append(
            RecordedCall(
                prompt=prompt,
                model=model,
                json_mode=json_mode,
                search=search,
                thinking_budget=thinking_budget,
            )
        )
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, GenerationResult):
            return item
        return GenerationResult(text=item)


def make_finder(
    script: list[str | GenerationResult | BaseException],
    *,
    min_probability: float = 0.4,
    sleep: Any = None,
) -> tuple[GeminiFinder, FakeGenerator, list[float]]:
    generator = FakeGenerator(script)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    finder = GeminiFinder(
        generator=generator,
        min_probability=min_probability,
        sleep=sleep or fake_sleep,
    )
    return finder, generator, sleeps


def entry() -> CatalogEntry:
    return CatalogEntry.model_validate(ENTRY_DATA)


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
    finder, generator, _ = make_finder(
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
    assert generator.calls[0].model == "gemini-3.1-flash-lite"
    assert "Bats in flight" in generator.calls[0].prompt


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
    finder, generator, sleeps = make_finder([error, '{"candidates": []}'])

    candidates = await finder.discover(entry())

    assert candidates == []
    assert len(generator.calls) == 2
    assert sleeps == [1.0]


async def test_generate_retries_on_timeout_with_backoff() -> None:
    finder, generator, sleeps = make_finder(
        [TimeoutError("request timed out"), '{"candidates": []}']
    )

    candidates = await finder.discover(entry())

    assert candidates == []
    assert len(generator.calls) == 2
    assert sleeps == [1.0]


async def test_generate_gives_up_after_four_attempts() -> None:
    error = RuntimeError("503 unavailable")
    finder, generator, sleeps = make_finder([error] * 5)

    assert await finder.discover(entry()) is None
    assert len(generator.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_discover_does_not_retry_on_malformed_json() -> None:
    finder, generator, _ = make_finder(
        [
            "not json",
            '{"candidates": [{"url": "https://a.example", "probability": 0.7}]}',
        ]
    )

    assert await finder.discover(entry()) is None
    assert len(generator.calls) == 1


async def test_discover_prompt_asks_for_the_exact_page() -> None:
    finder, generator, _ = make_finder(['{"candidates": []}'])

    await finder.discover(entry())

    prompt = generator.calls[0].prompt
    assert "exact page" in prompt
    assert "homepage" in prompt


async def test_discover_filters_candidates_below_min_probability() -> None:
    finder, _, _ = make_finder(
        [
            '{"candidates": ['
            '{"url": "https://strong.example/guide", "probability": 0.8},'
            '{"url": "https://borderline.example/guide", "probability": 0.4},'
            '{"url": "https://weak.example/guide", "probability": 0.39}'
            "]}"
        ],
        min_probability=0.4,
    )

    candidates = await finder.discover(entry())

    assert candidates == [
        Candidate("https://strong.example/guide", 0.8),
        Candidate("https://borderline.example/guide", 0.4),
    ]


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
        ("https://www.amazon.co.uk/dp/B0001", True),
        ("https://smile.amazon.co.uk/dp/B0001", True),
        ("https://amazon.com/dp/B0001", True),
        ("https://www.abebooks.co.uk/book/123", True),
        ("https://www.abebooks.com/book/123", True),
        ("https://www.alibris.co.uk/book/123", True),
        ("https://alibris.com/book/123", True),
        ("https://www.waterstones.com/book/123", True),
        ("https://www.foyles.co.uk/book/123", True),
        ("https://www.blackwells.co.uk/book/123", True),
        ("https://wordery.com/book/123", True),
        ("https://uk.bookshop.org/p/books/123", True),
        ("https://www.nhbs.com/title/123", False),
        ("https://www.pemberleybooks.com/product/123", False),
        ("https://www.summerfieldbooks.com/product/123", False),
    ],
)
def test_blocklist_uses_host_suffix_not_substring(url: str, blocked: bool) -> None:
    assert is_blocked_host(url) is blocked


async def test_discovery_prompt_forbids_dead_host_site_queries_and_lists_marketplaces() -> (
    None
):
    assert "site:" in DISCOVERY_PROMPT
    finder, generator, _ = make_finder(['{"candidates": []}'])

    await finder.discover(entry())

    prompt = generator.calls[0].prompt
    assert "site:" in prompt
    assert "example.org" in prompt
    for domain in (
        "amazon.co.uk",
        "amazon.com",
        "abebooks.co.uk",
        "abebooks.com",
        "alibris.co.uk",
        "alibris.com",
        "waterstones.com",
        "foyles.co.uk",
        "blackwells.co.uk",
        "wordery.com",
        "bookshop.org",
        "ebay.co.uk",
        "zvab.com",
        "scribd.com",
        "archive.org",
    ):
        assert domain in prompt


async def test_confirmation_prompt_includes_the_page_metadata() -> None:
    finder, generator, _ = make_finder(['{"is_match": true, "reasoning": "same"}'])

    await finder.confirm_alternative(
        entry(),
        url="https://shop.example/guide",
        page_title="Bats in flight",
        page_h1="Bat guide",
    )

    prompt = generator.calls[0].prompt
    assert "https://shop.example/guide" in prompt
    assert "Bats in flight" in prompt
    assert "Bat guide" in prompt


def test_missing_api_key_fails_fast() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiFinder(api_key=None)


async def test_confirmation_prompt_includes_page_text() -> None:
    finder, generator, _ = make_finder(['{"is_match": true, "reasoning": "same"}'])

    await finder.confirm_alternative(
        entry(),
        url="https://shop.example/guide",
        page_title="World Register of Marine Species",
        page_h1=None,
        page_snippet="Renk, H. (1984). Calorific values of plankton.",
    )

    prompt = generator.calls[0].prompt
    assert "Calorific values of plankton" in prompt


async def test_discover_hint_names_the_rejected_index() -> None:
    finder, generator, _ = make_finder(['{"candidates": []}'])

    await finder.discover(entry(), hint="https://example.org/keys/")

    prompt = generator.calls[0].prompt
    assert "https://example.org/keys/ is an index" in prompt


async def test_distinct_models_and_search_tool_scoping() -> None:
    generator = FakeGenerator(
        [
            '{"is_match": true, "reasoning": "matched"}',
            '{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}',
            '{"is_match": true, "reasoning": "confirmed"}',
        ]
    )
    finder = GeminiFinder(
        generator=generator,
        verification_model="gemini-3.1-flash-lite",
        discovery_model="gemini-3.8-flash",
    )

    # 1. verify_redirect: should use verification_model and disable search
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
    call1 = generator.calls[0]
    assert call1.model == "gemini-3.1-flash-lite"
    assert call1.search is False
    assert call1.json_mode is True
    assert call1.thinking_budget == 0
    assert "Comprehensive guide to bats in flight." in call1.prompt

    # 2. discover: should use discovery_model and enable search
    await finder.discover(entry())
    call2 = generator.calls[1]
    assert call2.model == "gemini-3.8-flash"
    assert call2.search is True
    assert call2.json_mode is True
    assert call2.thinking_budget == 512

    # 3. confirm_alternative: should use verification_model and disable search
    await finder.confirm_alternative(
        entry(),
        url="https://example.org/found",
        page_title="Bats in flight",
        page_h1="Bats in flight",
    )
    call3 = generator.calls[2]
    assert call3.model == "gemini-3.1-flash-lite"
    assert call3.search is False
    assert call3.json_mode is True
    assert call3.thinking_budget == 0


async def test_max_searches_ceiling_stops_discovery() -> None:
    generator = FakeGenerator(
        [
            GenerationResult(
                text='{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}',
                search_queries=2,
            )
        ]
    )
    finder = GeminiFinder(
        generator=generator,
        max_searches=2,
    )

    # First discover executes 2 searches, hitting the limit
    candidates1 = await finder.discover(entry())
    assert candidates1 is not None
    assert finder.total_searches == 2

    # Second discover aborts early because max_searches is reached
    candidates2 = await finder.discover(entry())
    assert candidates2 is None
    assert len(generator.calls) == 1


async def test_cost_tracker_captures_gemini_usage(tmp_path: Path) -> None:
    from idsignpost.cost import SessionCostTracker

    generator = FakeGenerator(
        [
            GenerationResult(
                text='{"is_match": true, "reasoning": "matched"}',
                prompt_tokens=120,
                output_tokens=30,
                cached_tokens=20,
                thinking_tokens=15,
                tool_use_prompt_tokens=10,
                search_queries=3,
            ),
            GenerationResult(
                text='{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}',
                prompt_tokens=200,
                output_tokens=50,
                thinking_tokens=45,
                tool_use_prompt_tokens=80,
                search_queries=2,
            ),
        ]
    )
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    finder = GeminiFinder(
        generator=generator,
        tracker=tracker,
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
    await finder.discover(entry())

    run = tracker.finish_run(command="test")
    assert run.calls == 2
    assert run.prompt_tokens == 320
    assert run.output_tokens == 80
    assert run.cached_tokens == 20
    assert run.thinking_tokens == 60
    assert run.tool_use_prompt_tokens == 90
    assert run.search_queries == 2
    assert "redirect_verification" in run.by_operation
    assert "candidate_discovery" in run.by_operation


async def test_genai_generator_configures_thinking_and_extracts_usage(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from google.genai import types
    from idsignpost.cost import SessionCostTracker
    from idsignpost.gemini_finder import _GenAIGenerator

    captured_configs: list[Any] = []
    responses = [
        SimpleNamespace(
            text='{"is_match": true, "reasoning": "matched"}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=20,
                cached_content_token_count=10,
                thoughts_token_count=0,
                tool_use_prompt_token_count=0,
            ),
            candidates=[],
        ),
        SimpleNamespace(
            text='{"candidates": [{"url": "https://example.org/found", "probability": 0.9}]}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=200,
                candidates_token_count=40,
                cached_content_token_count=0,
                thoughts_token_count=180,
                tool_use_prompt_token_count=75,
            ),
            candidates=[
                SimpleNamespace(
                    grounding_metadata=SimpleNamespace(
                        web_search_queries=["bats guide", "bats pdf"]
                    )
                )
            ],
        ),
        SimpleNamespace(
            text='{"is_match": true, "reasoning": "confirmed"}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=150,
                candidates_token_count=25,
                cached_content_token_count=0,
                thoughts_token_count=0,
                tool_use_prompt_token_count=0,
            ),
            candidates=[],
        ),
    ]

    class FakeModels:
        async def generate_content(
            self, *, model: str, contents: str, config: Any
        ) -> Any:
            captured_configs.append(config)
            return responses.pop(0)

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    finder = GeminiFinder(
        generator=_GenAIGenerator(client=fake_client),
        tracker=tracker,
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
    await finder.discover(entry())
    await finder.confirm_alternative(
        entry(),
        url="https://example.org/found",
        page_title="Bats",
        page_h1="Bats",
    )

    assert len(captured_configs) == 3
    assert captured_configs[0].thinking_config == types.ThinkingConfig(
        thinking_budget=0
    )
    assert captured_configs[1].thinking_config == types.ThinkingConfig(
        thinking_budget=512
    )
    assert captured_configs[2].thinking_config == types.ThinkingConfig(
        thinking_budget=0
    )

    run = tracker.finish_run(command="test")
    assert run.thinking_tokens == 180
    assert run.tool_use_prompt_tokens == 75
    assert run.search_queries == 2

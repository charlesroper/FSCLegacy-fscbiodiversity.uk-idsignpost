"""Unit tests for Gemini session cost calculation and persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from idsignpost.cost import (
    SessionCostTracker,
    calculate_cost,
    get_model_rates,
)


def test_get_model_rates_matches_prefixes() -> None:
    rates_31 = get_model_rates("gemini-3.1-flash-lite")
    assert rates_31 == (0.25, 1.50, 0.02, 14.0)

    rates_with_prefix = get_model_rates("models/gemini-3.1-flash-lite")
    assert rates_with_prefix == (0.25, 1.50, 0.02, 14.0)

    rates_25_lite = get_model_rates("gemini-2.5-flash-lite")
    assert rates_25_lite == (0.075, 0.30, 0.01875, 35.0)

    rates_38 = get_model_rates("gemini-3.8-flash")
    assert rates_38 == (0.50, 2.00, 0.05, 14.0)


def test_calculate_cost_tokens_and_searches() -> None:
    # 1,000,000 prompt tokens + 1,000,000 output tokens on 3.1-flash-lite = $0.25 + $1.50 = $1.75
    cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=1_000_000,
        output_tokens=1_000_000,
        cached_tokens=0,
        search_queries=0,
    )
    assert round(cost, 2) == 1.75

    # 1,000 search queries on 3.1-flash-lite = $14.00
    search_cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        search_queries=1_000,
    )
    assert round(search_cost, 2) == 14.00

    # 1,000 search queries on 2.5-flash = $35.00
    search_cost_25 = calculate_cost(
        "gemini-2.5-flash",
        prompt_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        search_queries=1_000,
    )
    assert round(search_cost_25, 2) == 35.00


def test_calculate_cost_with_cached_tokens() -> None:
    # 100,000 prompt tokens total, of which 80,000 are cached on 3.1-flash-lite
    # Uncached: 20,000 * $0.25 / 1M = $0.005
    # Cached: 80,000 * $0.02 / 1M = $0.0016
    cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=100_000,
        output_tokens=0,
        cached_tokens=80_000,
        search_queries=0,
    )
    assert round(cost, 6) == 0.0066


def test_session_cost_tracker_lifecycle(tmp_path: Path) -> None:
    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)

    tracker.record_call(
        operation="redirect_verification",
        model="gemini-3.1-flash-lite",
        prompt_tokens=1000,
        output_tokens=100,
        search_queries=0,
    )
    tracker.record_call(
        operation="candidate_discovery",
        model="gemini-3.1-flash-lite",
        prompt_tokens=500,
        output_tokens=50,
        search_queries=2,
    )

    run = tracker.finish_run(command="verify")
    assert run.calls == 2
    assert run.search_queries == 2
    assert run.prompt_tokens == 1500
    assert run.output_tokens == 150
    assert "redirect_verification" in run.by_operation
    assert "candidate_discovery" in run.by_operation

    # Check persistence
    assert cost_file.exists()
    loaded = SessionCostTracker.load(path=cost_file)
    assert len(loaded.runs) == 1
    assert loaded.session_total_calls == 2
    assert loaded.session_total_search_queries == 2
    assert round(loaded.session_total_cost, 4) == round(run.cost_usd, 4)


def test_format_run_summary(tmp_path: Path) -> None:
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    tracker.record_call(
        operation="redirect_verification",
        model="gemini-3.1-flash-lite",
        prompt_tokens=400,
        output_tokens=40,
    )
    run = tracker.finish_run(command="verify")
    summary = tracker.format_run_summary(run)
    assert "idsignpost verify cost breakdown:" in summary
    assert "Redirect checks" in summary
    assert "Run total:" in summary
    assert "Session grand total:" in summary


def test_format_session_table_empty(tmp_path: Path) -> None:
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    table = tracker.format_session_table()
    assert "No recorded runs in the current session." in table


def test_format_session_table_populated(tmp_path: Path) -> None:
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    tracker.record_call(
        operation="candidate_discovery",
        model="gemini-3.1-flash-lite",
        prompt_tokens=1000,
        output_tokens=200,
        search_queries=1,
    )
    tracker.finish_run(command="run")
    table = tracker.format_session_table()
    assert "Session started:" in table
    assert "run" in table
    assert "Total" in table


def test_session_expiration(tmp_path: Path) -> None:
    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)
    tracker.record_call(
        operation="candidate_confirmation",
        model="gemini-3.1-flash-lite",
        prompt_tokens=500,
        output_tokens=50,
    )
    tracker.finish_run(command="verify")

    # Manually backdate updated_at to 25 hours ago
    expired_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    tracker.updated_at = expired_time
    tracker.save()

    # Loading with 24-hour TTL should roll over into a fresh session
    fresh = SessionCostTracker.load(path=cost_file, ttl_hours=24.0)
    assert len(fresh.runs) == 0
    assert fresh.session_total_calls == 0


def test_session_reset(tmp_path: Path) -> None:
    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)
    tracker.record_call(
        operation="candidate_confirmation",
        model="gemini-3.1-flash-lite",
        prompt_tokens=500,
        output_tokens=50,
    )
    tracker.finish_run(command="verify")
    assert len(tracker.runs) == 1

    tracker.reset()
    assert len(tracker.runs) == 0
    loaded = SessionCostTracker.load(path=cost_file)
    assert len(loaded.runs) == 0

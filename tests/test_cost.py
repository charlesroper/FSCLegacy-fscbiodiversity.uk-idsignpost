"""Unit tests for Gemini session cost calculation and persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from typing import get_args

from idsignpost.cost import (
    OPERATION_LABELS,
    GeminiOperation,
    SessionCostTracker,
    calculate_cost,
    get_model_rates,
)


def test_operation_labels_cover_gemini_operation_literal() -> None:
    expected = {
        "redirect_verification",
        "candidate_discovery",
        "candidate_confirmation",
    }
    assert set(get_args(GeminiOperation)) == expected
    assert set(OPERATION_LABELS) == expected


def test_get_model_rates_matches_prefixes() -> None:
    assert get_model_rates("gemini-3.1-flash-lite") == (0.25, 1.50, 0.025, 14.0)
    assert get_model_rates("models/gemini-3.1-flash-lite") == (0.25, 1.50, 0.025, 14.0)
    assert get_model_rates("gemini-3.1-pro") == (2.00, 12.00, 0.20, 14.0)
    assert get_model_rates("gemini-3-flash") == (0.50, 3.00, 0.05, 14.0)
    assert get_model_rates("gemini-2.5-flash-lite") == (0.10, 0.40, 0.01, 35.0)
    assert get_model_rates("gemini-2.5-flash") == (0.30, 2.50, 0.03, 35.0)
    assert get_model_rates("gemini-2.5-pro") == (1.25, 10.00, 0.125, 35.0)
    assert get_model_rates("gemini-unknown-future") == (0.25, 1.50, 0.025, 14.0)
    assert get_model_rates("gemini-2.5-unknown") == (0.25, 1.50, 0.025, 35.0)


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

    # Gemini 3+: billed per executed query at $14.00 / 1,000
    search_cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        search_queries=4,
    )
    assert search_cost == 4 * 14.0 / 1_000.0

    # Gemini 2.5: billed per grounded prompt (1 unit whenever search_queries > 0) at $35.00 / 1,000
    search_cost_25 = calculate_cost(
        "gemini-2.5-flash",
        prompt_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        search_queries=4,
    )
    assert search_cost_25 == 35.0 / 1_000.0

    # Zero search queries incurs no search cost on either generation
    assert (
        calculate_cost(
            "gemini-2.5-flash",
            prompt_tokens=0,
            output_tokens=0,
            search_queries=0,
        )
        == 0.0
    )


def test_calculate_cost_with_cached_tokens() -> None:
    # 100,000 prompt tokens total, of which 80,000 are cached on 3.1-flash-lite
    # Uncached: 20,000 * $0.25 / 1M = $0.005
    # Cached: 80,000 * $0.025 / 1M = $0.002
    cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=100_000,
        output_tokens=0,
        cached_tokens=80_000,
        search_queries=0,
    )
    assert round(cost, 6) == 0.007


def test_calculate_cost_bills_thinking_and_tool_use_tokens() -> None:
    # On gemini-3.1-flash-lite: input=$0.25/1M, output=$1.50/1M, cached=$0.025/1M
    # Uncached prompt: (100_000 - 40_000) + 20_000 tool_use = 80_000 -> $0.02
    # Cached: 40_000 -> $0.001
    # Billed output: 10_000 output + 30_000 thinking = 40_000 -> $0.06
    cost = calculate_cost(
        "gemini-3.1-flash-lite",
        prompt_tokens=100_000,
        output_tokens=10_000,
        cached_tokens=40_000,
        thinking_tokens=30_000,
        tool_use_prompt_tokens=20_000,
        search_queries=0,
    )
    assert abs(cost - 0.081) < 1e-12


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


def test_load_strict_handles_missing_corrupt_and_directory(tmp_path: Path) -> None:
    import pytest

    missing = tmp_path / "missing.json"
    fresh = SessionCostTracker.load(missing, strict=True)
    assert fresh.runs == []

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert SessionCostTracker.load(corrupt).runs == []
    with pytest.raises(ValueError):
        SessionCostTracker.load(corrupt, strict=True)

    non_dict = tmp_path / "list.json"
    non_dict.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(ValueError):
        SessionCostTracker.load(non_dict, strict=True)

    bad_fields = tmp_path / "bad_fields.json"
    bad_fields.write_text('{"updated_at": 123}', encoding="utf-8")
    with pytest.raises(ValueError):
        SessionCostTracker.load(bad_fields, strict=True)

    directory = tmp_path / "cost_dir"
    directory.mkdir()
    assert SessionCostTracker.load(directory).runs == []
    with pytest.raises(OSError):
        SessionCostTracker.load(directory, strict=True)


def test_tracker_persists_thinking_and_tool_use_tokens_and_loads_legacy_json(
    tmp_path: Path,
) -> None:
    import json

    cost_file = tmp_path / "costs.json"
    tracker = SessionCostTracker(path=cost_file)
    tracker.record_call(
        operation="candidate_discovery",
        model="gemini-3.1-flash-lite",
        prompt_tokens=1_000,
        output_tokens=100,
        thinking_tokens=400,
        tool_use_prompt_tokens=250,
        search_queries=2,
    )
    run = tracker.finish_run(command="verify")

    assert run.thinking_tokens == 400
    assert run.tool_use_prompt_tokens == 250
    assert tracker.session_total_thinking_tokens == 400
    assert tracker.session_total_tool_use_prompt_tokens == 250

    payload = json.loads(cost_file.read_text(encoding="utf-8"))
    assert payload["runs"][0]["thinking_tokens"] == 400
    assert payload["runs"][0]["tool_use_prompt_tokens"] == 250
    op_payload = payload["runs"][0]["by_operation"]["candidate_discovery"]
    assert op_payload["thinking_tokens"] == 400
    assert op_payload["tool_use_prompt_tokens"] == 250

    reloaded = SessionCostTracker.load(cost_file)
    assert reloaded.session_total_thinking_tokens == 400
    assert reloaded.session_total_tool_use_prompt_tokens == 250

    # Legacy JSON lacking thinking_tokens and tool_use_prompt_tokens defaults to 0
    legacy_file = tmp_path / "legacy_costs.json"
    legacy_payload = {
        "version": 1,
        "started_at": payload["started_at"],
        "updated_at": payload["updated_at"],
        "runs": [
            {
                "timestamp": payload["runs"][0]["timestamp"],
                "command": "verify",
                "calls": 1,
                "prompt_tokens": 500,
                "output_tokens": 50,
                "cached_tokens": 0,
                "search_queries": 1,
                "cost_usd": 0.0142,
                "by_operation": {
                    "candidate_discovery": {
                        "calls": 1,
                        "prompt_tokens": 500,
                        "output_tokens": 50,
                        "cached_tokens": 0,
                        "search_queries": 1,
                        "cost_usd": 0.0142,
                    }
                },
            }
        ],
    }
    legacy_file.write_text(json.dumps(legacy_payload), encoding="utf-8")
    legacy_loaded = SessionCostTracker.load(legacy_file)
    assert legacy_loaded.runs[0].thinking_tokens == 0
    assert legacy_loaded.runs[0].tool_use_prompt_tokens == 0
    legacy_op = legacy_loaded.runs[0].by_operation["candidate_discovery"]
    assert legacy_op.thinking_tokens == 0
    assert legacy_op.tool_use_prompt_tokens == 0


def test_format_reports_display_combined_billed_input_and_output(
    tmp_path: Path,
) -> None:
    tracker = SessionCostTracker(path=tmp_path / "costs.json")
    tracker.record_call(
        operation="candidate_discovery",
        model="gemini-3.1-flash-lite",
        prompt_tokens=1_000,
        output_tokens=150,
        thinking_tokens=350,
        tool_use_prompt_tokens=200,
        search_queries=1,
    )
    run = tracker.finish_run(command="verify")

    summary = tracker.format_run_summary(run)
    # Combined input: 1,000 + 200 = 1,200; combined output: 150 + 350 = 500
    assert "1,200 in,    500 out" in summary

    table = tracker.format_session_table()
    assert "1,200" in table
    assert "500" in table

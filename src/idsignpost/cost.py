"""Local session cost tracking and pricing calculation for Gemini operations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from idsignpost.export import atomic_write

DEFAULT_COST_FILE = ".idsignpost_costs.json"
DEFAULT_SESSION_TTL_HOURS = 24.0

OPERATION_LABELS: dict[str, str] = {
    "redirect_verification": "Redirect checks",
    "candidate_discovery": "Discovery searches",
    "candidate_confirmation": "Confirmations",
}

# Standard rates per 1,000,000 tokens (input, output, cached)
# and per 1,000 Google Search grounding queries.
MODEL_RATES: dict[str, tuple[float, float, float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.02, 14.0),
    "gemini-2.5-flash-lite": (0.075, 0.30, 0.01875, 35.0),
    "gemini-2.5-flash": (0.15, 0.60, 0.0375, 35.0),
    "gemini-3.5-flash-lite": (0.25, 1.50, 0.02, 14.0),
    "gemini-3.5-flash": (0.50, 2.00, 0.05, 14.0),
    "gemini-3.8-flash": (0.50, 2.00, 0.05, 14.0),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def get_model_rates(model: str) -> tuple[float, float, float, float]:
    """Return (input_per_m, output_per_m, cached_per_m, search_per_k) for model."""
    clean = model.removeprefix("models/").lower()
    for prefix, rates in MODEL_RATES.items():
        if clean.startswith(prefix):
            return rates
    search_rate = 35.0 if any(tag in clean for tag in ("2.5", "2.0", "1.5")) else 14.0
    return (0.25, 1.50, 0.02, search_rate)


def calculate_cost(
    model: str,
    *,
    prompt_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    search_queries: int = 0,
) -> float:
    """Calculate nominal USD cost from token and search query counts."""
    inp_m, out_m, cache_m, search_k = get_model_rates(model)
    uncached_prompt = max(0, prompt_tokens - cached_tokens)
    token_cost = (
        (uncached_prompt * inp_m / 1_000_000.0)
        + (cached_tokens * cache_m / 1_000_000.0)
        + (output_tokens * out_m / 1_000_000.0)
    )
    search_cost = search_queries * search_k / 1_000.0
    return token_cost + search_cost


@dataclass(slots=True)
class CallRecord:
    """Record of a single completed Gemini API call."""

    operation: str
    model: str
    prompt_tokens: int
    output_tokens: int
    cached_tokens: int
    search_queries: int
    cost_usd: float


@dataclass(slots=True)
class OperationStats:
    """Aggregated stats for one operation type."""

    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    search_queries: int = 0
    cost_usd: float = 0.0

    def add(self, record: CallRecord) -> None:
        self.calls += 1
        self.prompt_tokens += record.prompt_tokens
        self.output_tokens += record.output_tokens
        self.cached_tokens += record.cached_tokens
        self.search_queries += record.search_queries
        self.cost_usd += record.cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "search_queries": self.search_queries,
            "cost_usd": round(self.cost_usd, 6),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationStats:
        return cls(
            calls=data.get("calls", 0),
            prompt_tokens=data.get("prompt_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cached_tokens=data.get("cached_tokens", 0),
            search_queries=data.get("search_queries", 0),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


@dataclass(slots=True)
class RunRecord:
    """Summary of all Gemini calls executed in one CLI command run."""

    timestamp: str
    command: str
    calls: int
    prompt_tokens: int
    output_tokens: int
    cached_tokens: int
    search_queries: int
    cost_usd: float
    by_operation: dict[str, OperationStats] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "command": self.command,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "search_queries": self.search_queries,
            "cost_usd": round(self.cost_usd, 6),
            "by_operation": {k: v.to_dict() for k, v in self.by_operation.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        by_op = {
            k: OperationStats.from_dict(v)
            for k, v in data.get("by_operation", {}).items()
        }
        return cls(
            timestamp=data.get("timestamp", ""),
            command=data.get("command", ""),
            calls=data.get("calls", 0),
            prompt_tokens=data.get("prompt_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cached_tokens=data.get("cached_tokens", 0),
            search_queries=data.get("search_queries", 0),
            cost_usd=float(data.get("cost_usd", 0.0)),
            by_operation=by_op,
        )


class SessionCostTracker:
    """Manages local session cost accumulation and persistence."""

    def __init__(
        self,
        path: Path | str = DEFAULT_COST_FILE,
        *,
        started_at: str | None = None,
        updated_at: str | None = None,
        runs: list[RunRecord] | None = None,
    ) -> None:
        self.path = Path(path)
        now = isoformat(utcnow())
        self.started_at = started_at or now
        self.updated_at = updated_at or now
        self.runs: list[RunRecord] = runs or []
        self._current_calls: list[CallRecord] = []

    def record_call(
        self,
        *,
        operation: str,
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        search_queries: int = 0,
    ) -> CallRecord:
        """Record usage and cost for one Gemini call."""
        cost = calculate_cost(
            model,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            search_queries=search_queries,
        )
        record = CallRecord(
            operation=operation,
            model=model,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            search_queries=search_queries,
            cost_usd=cost,
        )
        self._current_calls.append(record)
        return record

    def finish_run(self, command: str) -> RunRecord:
        """Aggregate current calls into a RunRecord, update session, and save."""
        now = isoformat(utcnow())
        by_op: dict[str, OperationStats] = {}
        total_prompt = 0
        total_output = 0
        total_cached = 0
        total_search = 0
        total_cost = 0.0

        for call in self._current_calls:
            stats = by_op.setdefault(call.operation, OperationStats())
            stats.add(call)
            total_prompt += call.prompt_tokens
            total_output += call.output_tokens
            total_cached += call.cached_tokens
            total_search += call.search_queries
            total_cost += call.cost_usd

        run = RunRecord(
            timestamp=now,
            command=command,
            calls=len(self._current_calls),
            prompt_tokens=total_prompt,
            output_tokens=total_output,
            cached_tokens=total_cached,
            search_queries=total_search,
            cost_usd=total_cost,
            by_operation=by_op,
        )
        self.runs.append(run)
        self.updated_at = now
        self._current_calls = []
        self.save()
        return run

    @property
    def session_total_cost(self) -> float:
        return sum(run.cost_usd for run in self.runs)

    @property
    def session_total_calls(self) -> int:
        return sum(run.calls for run in self.runs)

    @property
    def session_total_prompt_tokens(self) -> int:
        return sum(run.prompt_tokens for run in self.runs)

    @property
    def session_total_output_tokens(self) -> int:
        return sum(run.output_tokens for run in self.runs)

    @property
    def session_total_search_queries(self) -> int:
        return sum(run.search_queries for run in self.runs)

    def format_run_summary(self, run: RunRecord) -> str:
        """Format itemised breakdown for the completed run and session total."""
        lines: list[str] = [f"idsignpost {run.command} cost breakdown:"]
        if not run.by_operation and run.calls == 0:
            lines.append(
                f"  0 Gemini calls, $0.00000 (Session total: "
                f"${self.session_total_cost:0.5f} across {len(self.runs)} runs)"
            )
            return "\n".join(lines)

        for op_key, label in OPERATION_LABELS.items():
            stats = run.by_operation.get(op_key)
            if stats and stats.calls > 0:
                lines.append(
                    f"  {label:<20} {stats.calls:>3} calls, "
                    f"{stats.prompt_tokens:>7,d} in, {stats.output_tokens:>6,d} out, "
                    f"{stats.search_queries:>3} searches, ${stats.cost_usd:0.5f}"
                )

        lines.append(
            f"  {'Run total:':<20} {run.calls:>3} calls, "
            f"{run.prompt_tokens:>7,d} in, {run.output_tokens:>6,d} out, "
            f"{run.search_queries:>3} searches, ${run.cost_usd:0.5f}"
        )
        lines.append(
            f"  {'Session grand total:':<20} {self.session_total_calls:>3} calls, "
            f"{self.session_total_prompt_tokens:>7,d} in, "
            f"{self.session_total_output_tokens:>6,d} out, "
            f"{self.session_total_search_queries:>3} searches, "
            f"${self.session_total_cost:0.5f} across {len(self.runs)} runs"
        )
        return "\n".join(lines)

    def format_session_table(self) -> str:
        """Format an aligned terminal table showing session history."""
        if not self.runs:
            return "No recorded runs in the current session."

        run_count = len(self.runs)
        run_label = "run" if run_count == 1 else "runs"
        lines: list[str] = [
            f"Session started: {self.started_at} ({run_count} {run_label})\n"
        ]
        header = (
            f"{'Run':<4} {'Timestamp':<20} {'Command':<10} {'Calls':>6} "
            f"{'Prompt Tok':>11} {'Output Tok':>11} {'Searches':>9} {'Run Cost':>11}"
        )
        divider = "-" * len(header)
        lines.append(header)
        lines.append(divider)

        for idx, run in enumerate(self.runs, 1):
            ts = run.timestamp[:19].replace("T", " ")
            lines.append(
                f"{idx:<4} {ts:<20} {run.command:<10} {run.calls:>6} "
                f"{run.prompt_tokens:>11,d} {run.output_tokens:>11,d} "
                f"{run.search_queries:>9} ${run.cost_usd:>10.5f}"
            )

        lines.append(divider)
        lines.append(
            f"{'Total':<36} {self.session_total_calls:>6} "
            f"{self.session_total_prompt_tokens:>11,d} "
            f"{self.session_total_output_tokens:>11,d} "
            f"{self.session_total_search_queries:>9} "
            f"${self.session_total_cost:>10.5f}"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "runs": [run.to_dict() for run in self.runs],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def save(self) -> None:
        """Persist session records to file atomically."""
        data = self.to_json()
        atomic_write(self.path, lambda handle: handle.write(data))

    @classmethod
    def load(
        cls,
        path: Path | str = DEFAULT_COST_FILE,
        *,
        ttl_hours: float = DEFAULT_SESSION_TTL_HOURS,
    ) -> SessionCostTracker:
        """Load session records from file, rolling over if expired."""
        target = Path(path)
        if not target.is_file():
            return cls(path=target)

        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return cls(path=target)

            updated_raw = payload.get("updated_at")
            if updated_raw:
                updated_dt = datetime.fromisoformat(updated_raw)
                if utcnow() - updated_dt > timedelta(hours=ttl_hours):
                    # Inactivity limit exceeded, start fresh session
                    return cls(path=target)

            runs = [
                RunRecord.from_dict(r)
                for r in payload.get("runs", [])
                if isinstance(r, dict)
            ]
            return cls(
                path=target,
                started_at=payload.get("started_at"),
                updated_at=payload.get("updated_at"),
                runs=runs,
            )
        except Exception:
            return cls(path=target)

    def reset(self) -> None:
        """Reset running session counters and persist fresh state."""
        now = isoformat(utcnow())
        self.started_at = now
        self.updated_at = now
        self.runs = []
        self._current_calls = []
        self.save()

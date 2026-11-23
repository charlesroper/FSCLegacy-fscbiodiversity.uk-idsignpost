"""Command line interface for idsignpost."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys

from idsignpost import __version__
from idsignpost.cost import SessionCostTracker
from idsignpost.export import (
    catalog_json,
    write_csv,
    write_json,
    write_markdown,
    write_parquet,
)
from idsignpost.extract import extract_catalog, merge_verification
from idsignpost.models import CatalogEntry, load_catalog
from idsignpost.pipeline import (
    OfflineRunError,
    VerifyConfig,
    VerifyDeps,
    VerifyStats,
    run_verification,
)

COMMANDS = {
    "extract": "Parse legacy HTML into the canonical catalog.",
    "verify": "Check links, archives, and alternatives.",
    "export": "Render CSV, Parquet, and Markdown.",
    "run": "Extract, verify, and export in one pass.",
    "cost": "Inspect local session costs and run history.",
}


def _merge_previous(
    entries: list[CatalogEntry], output: Path, command: str
) -> list[CatalogEntry]:
    if not output.exists():
        return entries
    try:
        return merge_verification(entries, load_catalog(output))
    except ValueError:
        print(
            f"idsignpost {command}: {output} is not a canonical catalog; "
            "verification fields will be reset",
            file=sys.stderr,
        )
        return entries


def _cmd_extract(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        print("idsignpost extract: --limit must be at least 1", file=sys.stderr)
        return 2

    entries = extract_catalog(Path(args.html_dir), limit=args.limit)
    if args.limit is not None and args.output is None:
        print(catalog_json(entries), end="")
        return 0

    output = Path(args.output or "idsignpost.json")
    entries = _merge_previous(entries, output, "extract")
    write_json(entries, output)
    print(
        f"idsignpost extract: wrote {len(entries)} entries to {output}", file=sys.stderr
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    try:
        entries = load_catalog(args.input)
        write_csv(entries, args.csv)
        write_parquet(entries, args.parquet)
        write_markdown(entries, args.md)
    except (OSError, ValueError) as exc:
        print(f"idsignpost export: {exc}", file=sys.stderr)
        return 1
    print(
        f"idsignpost export: wrote {len(entries)} entries to "
        f"{args.csv}, {args.parquet}, {args.md}",
        file=sys.stderr,
    )
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    try:
        tracker = SessionCostTracker.load(args.cost_file, strict=not args.reset)
        if args.reset:
            tracker.reset()
            print("idsignpost cost: session history reset", file=sys.stderr)
            return 0
        if args.json:
            print(tracker.to_json())
            return 0
        print(tracker.format_session_table())
        return 0
    except (OSError, ValueError) as exc:
        print(f"idsignpost cost: {exc}", file=sys.stderr)
        return 1


def _verify_config(args: argparse.Namespace) -> VerifyConfig:
    return VerifyConfig(
        command=args.command,
        cache_file=args.cache_file,
        cache_ttl_days=args.cache_ttl_days,
        cost_file=args.cost_file,
        sample=args.sample,
        seed=args.seed,
        limit=args.limit,
        only_broken=args.only_broken,
        save_archive=args.save_archive,
        no_ia_keys=args.no_ia_keys,
        refresh=args.refresh,
        concurrency=args.concurrency,
        wayback_concurrency=args.wayback_concurrency,
        gemini_concurrency=args.gemini_concurrency,
        timeout=args.timeout,
        min_prob=args.min_prob,
        model=args.model,
        verification_model=args.verification_model,
        discovery_model=args.discovery_model,
        max_searches=args.max_searches,
        api_key=os.environ.get("GEMINI_API_KEY"),
        ia_access_key=os.environ.get("IA_ACCESS_KEY"),
        ia_secret_key=os.environ.get("IA_SECRET_KEY"),
    )


def _print_summary(stats: VerifyStats) -> None:
    states = stats.states
    print(
        f"idsignpost verify: {stats.checked} checks, "
        f"{states.get('ok', 0)} ok, {states.get('broken', 0)} broken, "
        f"{states.get('blocked', 0)} blocked, "
        f"{states.get('unverified', 0)} unverified, "
        f"{stats.alternatives} alternatives, "
        f"{stats.wayback_captures} captures, {stats.cache_hits} cache hits",
        file=sys.stderr,
    )
    if stats.cost_summary:
        print(stats.cost_summary, file=sys.stderr)


def _run_verify_stage(
    args: argparse.Namespace,
    entries: list[CatalogEntry],
    config: VerifyConfig,
    *,
    deps: VerifyDeps | None = None,
) -> tuple[list[CatalogEntry] | None, int]:
    try:
        merged, stats = asyncio.run(run_verification(entries, config, deps=deps))
    except KeyboardInterrupt:
        print(
            f"idsignpost {args.command}: interrupted, URL cache saved",
            file=sys.stderr,
        )
        return None, 130
    except OfflineRunError as exc:
        print(f"idsignpost {args.command}: aborted, {exc}", file=sys.stderr)
        return None, 1
    except ValueError as exc:
        print(f"idsignpost {args.command}: {exc}", file=sys.stderr)
        return None, 1
    write_json(merged, args.output)
    _print_summary(stats)
    return merged, 0


def _missing_keys(command: str, config: VerifyConfig) -> bool:
    if not config.api_key:
        print(
            f"idsignpost {command}: GEMINI_API_KEY is required for verification",
            file=sys.stderr,
        )
        return True
    if config.save_archive and not config.no_ia_keys:
        if not (config.ia_access_key and config.ia_secret_key):
            print(
                f"idsignpost {command}: IA_ACCESS_KEY and IA_SECRET_KEY are required "
                "with --save-archive (or pass --no-ia-keys for unauthenticated captures)",
                file=sys.stderr,
            )
            return True
    return False


def _prepare_verify(
    args: argparse.Namespace, command: str
) -> tuple[VerifyConfig | None, int]:
    config = _verify_config(args)
    error = config.validate()
    if error is not None:
        print(f"idsignpost {command}: {error}", file=sys.stderr)
        return None, 2
    if _missing_keys(command, config):
        return None, 1
    return config, 0


def _cmd_verify(
    args: argparse.Namespace,
    *,
    deps: VerifyDeps | None = None,
) -> int:
    config, code = _prepare_verify(args, "verify")
    if config is None:
        return code
    try:
        entries = load_catalog(args.input)
    except (OSError, ValueError) as exc:
        print(f"idsignpost verify: cannot load {args.input}: {exc}", file=sys.stderr)
        return 1
    _, code = _run_verify_stage(args, entries, config, deps=deps)
    return code


def _cmd_run(
    args: argparse.Namespace,
    *,
    deps: VerifyDeps | None = None,
) -> int:
    config, code = _prepare_verify(args, "run")
    if config is None:
        return code

    try:
        entries = extract_catalog(Path(args.html_dir))
        entries = _merge_previous(entries, Path(args.output), "run")
    except (OSError, ValueError) as exc:
        print(f"idsignpost run: cannot build the catalog: {exc}", file=sys.stderr)
        return 1

    merged, code = _run_verify_stage(args, entries, config, deps=deps)
    if merged is None:
        return code
    write_csv(merged, args.csv)
    write_parquet(merged, args.parquet)
    write_markdown(merged, args.md)
    print(
        f"idsignpost run: wrote {len(merged)} entries to "
        f"{args.output}, {args.csv}, {args.parquet}, {args.md}",
        file=sys.stderr,
    )
    return 0


def _add_verify_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        default="idsignpost.json",
        help="Output catalog JSON path (default: idsignpost.json).",
    )
    parser.add_argument(
        "--cache-file",
        default=".url_cache.json",
        help="URL and entry cache file path (default: .url_cache.json).",
    )
    parser.add_argument(
        "--cache-ttl-days",
        type=int,
        default=30,
        help="Cache time-to-live in days (default: 30).",
    )
    parser.add_argument(
        "--cost-file",
        default=".idsignpost_costs.json",
        help="Local cost tracking file path (default: .idsignpost_costs.json).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        help="Verify a stratified cross-section sample of N entries.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for stratified sampling (default: 42).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Verify only the first N entries.",
    )
    parser.add_argument(
        "--only-broken",
        action="store_true",
        help="Verify only entries currently marked broken.",
    )
    parser.add_argument(
        "--save-archive",
        action="store_true",
        help="Trigger Wayback Save Page Now for unarchived live URLs.",
    )
    parser.add_argument(
        "--no-ia-keys",
        action="store_true",
        help="Fall back to unauthenticated Save Page Now without IA_ACCESS_KEY and IA_SECRET_KEY.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached results and force re-verification.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Maximum concurrent HTTP workers (default: 10).",
    )
    parser.add_argument(
        "--wayback-concurrency",
        type=int,
        default=2,
        help="Maximum concurrent Wayback workers (default: 2).",
    )
    parser.add_argument(
        "--gemini-concurrency",
        type=int,
        default=3,
        help="Maximum concurrent Gemini workers (default: 3).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP and Wayback timeout in seconds (default: 10.0).",
    )
    parser.add_argument(
        "--min-prob",
        type=float,
        default=0.40,
        help="Minimum alternative probability score from 0.0 to 1.0 (default: 0.40).",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.1-flash-lite",
        help="Default Gemini model name (default: gemini-3.1-flash-lite).",
    )
    parser.add_argument(
        "--verification-model",
        help="Gemini model for redirect and confirmation checks (default: matches --model).",
    )
    parser.add_argument(
        "--discovery-model",
        help="Gemini model for grounded candidate discovery (default: matches --model).",
    )
    parser.add_argument(
        "--max-searches",
        type=int,
        help="Maximum Google Search grounding queries allowed across the run.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )


def _add_export_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--csv",
        default="idsignpost.csv",
        help="Output CSV file path (default: idsignpost.csv).",
    )
    parser.add_argument(
        "--parquet",
        default="idsignpost.parquet",
        help="Output Apache Parquet file path (default: idsignpost.parquet).",
    )
    parser.add_argument(
        "--md",
        default="idsignpost.md",
        help="Output Markdown file path (default: idsignpost.md).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idsignpost",
        description="Build and verify a catalog of biodiversity identification resources.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser(
        "extract", help=COMMANDS["extract"], description=COMMANDS["extract"]
    )
    extract_parser.add_argument(
        "--html-dir", default="idsignpost", help="Directory of legacy HTML pages."
    )
    extract_parser.add_argument(
        "--output",
        help="Catalog JSON path (default: idsignpost.json). With --limit and no "
        "explicit path, the sample goes to stdout.",
    )
    extract_parser.add_argument(
        "--limit", type=int, help="Parse only the first N files and print to stdout."
    )
    extract_parser.set_defaults(handler=_cmd_extract)

    verify_parser = subparsers.add_parser(
        "verify", help=COMMANDS["verify"], description=COMMANDS["verify"]
    )
    verify_parser.add_argument(
        "--input",
        default="idsignpost.json",
        help="Input catalog JSON path (default: idsignpost.json).",
    )
    _add_verify_options(verify_parser)
    verify_parser.set_defaults(handler=_cmd_verify)

    export_parser = subparsers.add_parser(
        "export", help=COMMANDS["export"], description=COMMANDS["export"]
    )
    export_parser.add_argument(
        "--input",
        default="idsignpost.json",
        help="Input catalog JSON path (default: idsignpost.json).",
    )
    _add_export_options(export_parser)
    export_parser.set_defaults(handler=_cmd_export)

    run_parser = subparsers.add_parser(
        "run", help=COMMANDS["run"], description=COMMANDS["run"]
    )
    run_parser.add_argument(
        "--html-dir", default="idsignpost", help="Directory of legacy HTML pages."
    )
    _add_verify_options(run_parser)
    _add_export_options(run_parser)
    run_parser.set_defaults(handler=_cmd_run)

    cost_parser = subparsers.add_parser(
        "cost", help=COMMANDS["cost"], description=COMMANDS["cost"]
    )
    cost_parser.add_argument(
        "--cost-file",
        default=".idsignpost_costs.json",
        help="Local cost tracking file path (default: .idsignpost_costs.json).",
    )
    cost_parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset running session costs and start a fresh session.",
    )
    cost_parser.add_argument(
        "--json",
        action="store_true",
        help="Output session history in machine-readable JSON format.",
    )
    cost_parser.set_defaults(handler=_cmd_cost)
    return parser


def _parse_env_value(raw_value: str) -> str:
    stripped = raw_value.strip()
    if not stripped:
        return ""
    if stripped[0] in {"'", '"'}:
        quote = stripped[0]
        closing = stripped.find(quote, 1)
        if closing != -1:
            return stripped[1:closing]
    return stripped.split("#", 1)[0].strip()


def _load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            if (
                cleaned.startswith("export")
                and len(cleaned) > 6
                and cleaned[6].isspace()
            ):
                cleaned = cleaned[6:].lstrip()
            if "=" not in cleaned:
                continue
            key, val = cleaned.split("=", 1)
            key = key.strip()
            parsed = _parse_env_value(val)
            if key and key not in os.environ:
                os.environ[key] = parsed
    except OSError:
        pass


def main(
    argv: list[str] | None = None,
    *,
    deps: VerifyDeps | None = None,
) -> int:
    if "PYTEST_CURRENT_TEST" not in os.environ:
        _load_env_file()
    args = build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    if args.command in {"verify", "run"}:
        return int(args.handler(args, deps=deps))
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

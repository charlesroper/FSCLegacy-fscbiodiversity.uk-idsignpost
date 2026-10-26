"""Command line interface for idsignpost."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from idsignpost import __version__
from idsignpost.cache import CatalogCache
from idsignpost.export import (
    catalog_json,
    write_csv,
    write_json,
    write_markdown,
    write_parquet,
)
from idsignpost.extract import extract_catalog, merge_verification
from idsignpost.gemini_finder import GeminiFinder
from idsignpost.http_checker import HTTPChecker
from idsignpost.models import CatalogEntry, load_catalog
from idsignpost.pipeline import (
    OfflineRunError,
    VerifyStats,
    Verifier,
    merge_verified,
)
from idsignpost.sampler import get_stratified_sample
from idsignpost.wayback import WaybackClient

COMMANDS = {
    "extract": "Parse legacy HTML into the canonical catalog.",
    "verify": "Check links, archives, and alternatives.",
    "export": "Render CSV, Parquet, and Markdown.",
    "run": "Extract, verify, and export in one pass.",
}


def _cmd_extract(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        print("idsignpost extract: --limit must be at least 1", file=sys.stderr)
        return 2

    entries = extract_catalog(Path(args.html_dir), limit=args.limit)
    if args.limit is not None and args.output is None:
        print(catalog_json(entries), end="")
        return 0

    output = Path(args.output or "idsignpost.json")
    if output.exists():
        try:
            entries = merge_verification(entries, load_catalog(output))
        except ValueError:
            print(
                f"idsignpost extract: {output} is not a canonical catalog; "
                "verification fields will be reset",
                file=sys.stderr,
            )
    write_json(entries, output)
    print(
        f"idsignpost extract: wrote {len(entries)} entries to {output}", file=sys.stderr
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    entries = load_catalog(args.input)
    write_csv(entries, args.csv)
    write_parquet(entries, args.parquet)
    write_markdown(entries, args.md)
    print(
        f"idsignpost export: wrote {len(entries)} entries to "
        f"{args.csv}, {args.parquet}, {args.md}",
        file=sys.stderr,
    )
    return 0


def _usage_error(args: argparse.Namespace) -> str | None:
    if args.sample is not None and args.limit is not None:
        return "--sample and --limit are mutually exclusive"
    if args.sample is not None and args.sample < 1:
        return "--sample must be at least 1"
    if args.limit is not None and args.limit < 1:
        return "--limit must be at least 1"
    if not 0.0 <= args.min_prob <= 1.0:
        return "--min-prob must be between 0 and 1"
    for name, label in (
        ("concurrency", "--concurrency"),
        ("wayback_concurrency", "--wayback-concurrency"),
        ("gemini_concurrency", "--gemini-concurrency"),
    ):
        if getattr(args, name) < 1:
            return f"{label} must be at least 1"
    if args.timeout <= 0:
        return "--timeout must be positive"
    if args.cache_ttl_days < 0:
        return "--cache-ttl-days must not be negative"
    return None


def select_targets(
    entries: list[CatalogEntry], args: argparse.Namespace
) -> list[CatalogEntry]:
    """Apply only-broken, then sample or limit."""
    pool = [
        entry
        for entry in entries
        if not args.only_broken or entry.web_link_state == "broken"
    ]
    if args.sample is not None:
        return get_stratified_sample(pool, args.sample, args.seed)
    if args.limit is not None:
        return pool[: args.limit]
    return pool


async def _verify_async(
    args: argparse.Namespace, targets: list[CatalogEntry]
) -> tuple[list[CatalogEntry], VerifyStats]:
    checker = HTTPChecker(timeout=args.timeout)
    wayback = WaybackClient(timeout=args.timeout, concurrency=args.wayback_concurrency)
    finder = GeminiFinder(
        model=args.model,
        api_key=os.environ.get("GEMINI_API_KEY"),
        min_probability=args.min_prob,
    )
    cache = CatalogCache.load(args.cache_file, ttl_days=args.cache_ttl_days)
    verifier = Verifier(
        cache=cache,
        checker=checker,
        finder=finder,
        wayback=wayback,
        concurrency=args.concurrency,
        gemini_concurrency=args.gemini_concurrency,
        save_archive=args.save_archive,
        refresh=args.refresh,
    )
    try:
        updated = await verifier.verify(targets)
    except KeyboardInterrupt:
        cache.save()
        raise
    finally:
        await checker.aclose()
        await wayback.aclose()
    cache.save()
    return updated, verifier.stats


def _verify_catalog(
    args: argparse.Namespace, targets: list[CatalogEntry]
) -> tuple[list[CatalogEntry], VerifyStats]:
    """Run verification; tests replace this hook."""
    return asyncio.run(_verify_async(args, targets))


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


def _verify_entries(
    args: argparse.Namespace, entries: list[CatalogEntry]
) -> tuple[list[CatalogEntry], VerifyStats] | None:
    targets = select_targets(entries, args)
    try:
        verified, stats = _verify_catalog(args, targets)
        merged = merge_verified(entries, verified)
    except OfflineRunError as exc:
        print(f"idsignpost {args.command}: aborted, {exc}", file=sys.stderr)
        return None
    except ValueError as exc:
        print(f"idsignpost {args.command}: {exc}", file=sys.stderr)
        return None
    write_json(merged, args.output)
    _print_summary(stats)
    return merged, stats


def _missing_key(command: str) -> bool:
    if os.environ.get("GEMINI_API_KEY"):
        return False
    print(
        f"idsignpost {command}: GEMINI_API_KEY is required for verification",
        file=sys.stderr,
    )
    return True


def _cmd_verify(args: argparse.Namespace) -> int:
    error = _usage_error(args)
    if error is not None:
        print(f"idsignpost verify: {error}", file=sys.stderr)
        return 2
    if _missing_key("verify"):
        return 1
    try:
        entries = load_catalog(args.input)
    except (OSError, ValueError) as exc:
        print(f"idsignpost verify: cannot load {args.input}: {exc}", file=sys.stderr)
        return 1
    return 0 if _verify_entries(args, entries) is not None else 1


def _cmd_run(args: argparse.Namespace) -> int:
    error = _usage_error(args)
    if error is not None:
        print(f"idsignpost run: {error}", file=sys.stderr)
        return 2
    if _missing_key("run"):
        return 1

    try:
        entries = extract_catalog(Path(args.html_dir))
        output = Path(args.output)
        if output.exists():
            entries = merge_verification(entries, load_catalog(output))
    except (OSError, ValueError) as exc:
        print(f"idsignpost run: cannot build the catalog: {exc}", file=sys.stderr)
        return 1

    verified = _verify_entries(args, entries)
    if verified is None:
        return 1
    merged, _ = verified
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
    parser.add_argument("--output", default="idsignpost.json")
    parser.add_argument("--cache-file", default=".url_cache.json")
    parser.add_argument("--cache-ttl-days", type=int, default=30)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-broken", action="store_true")
    parser.add_argument("--save-archive", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--wayback-concurrency", type=int, default=2)
    parser.add_argument("--gemini-concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--min-prob", type=float, default=0.40)
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("-v", "--verbose", action="store_true")


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
    verify_parser.add_argument("--input", default="idsignpost.json")
    _add_verify_options(verify_parser)
    verify_parser.set_defaults(handler=_cmd_verify)

    export_parser = subparsers.add_parser(
        "export", help=COMMANDS["export"], description=COMMANDS["export"]
    )
    export_parser.add_argument("--input", default="idsignpost.json")
    export_parser.add_argument("--csv", default="idsignpost.csv")
    export_parser.add_argument("--parquet", default="idsignpost.parquet")
    export_parser.add_argument("--md", default="idsignpost.md")
    export_parser.set_defaults(handler=_cmd_export)

    run_parser = subparsers.add_parser(
        "run", help=COMMANDS["run"], description=COMMANDS["run"]
    )
    run_parser.add_argument(
        "--html-dir", default="idsignpost", help="Directory of legacy HTML pages."
    )
    _add_verify_options(run_parser)
    run_parser.add_argument("--csv", default="idsignpost.csv")
    run_parser.add_argument("--parquet", default="idsignpost.parquet")
    run_parser.add_argument("--md", default="idsignpost.md")
    run_parser.set_defaults(handler=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line interface for idsignpost."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from idsignpost import __version__
from idsignpost.extract import (
    catalog_json,
    extract_catalog,
    merge_verification,
    write_catalog,
)
from idsignpost.models import load_catalog

COMMANDS = {
    "extract": "Parse legacy HTML into the canonical catalog.",
    "verify": "Check links, archives, and alternatives.",
    "export": "Render CSV, Parquet, and Markdown.",
    "run": "Extract, verify, and export in one pass.",
}


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"idsignpost {args.command}: not implemented yet", file=sys.stderr)
    return 1


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
        entries = merge_verification(entries, load_catalog(output))
    write_catalog(entries, output)
    print(
        f"idsignpost extract: wrote {len(entries)} entries to {output}", file=sys.stderr
    )
    return 0


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

    for name in ("verify", "export", "run"):
        subparser = subparsers.add_parser(
            name, help=COMMANDS[name], description=COMMANDS[name]
        )
        subparser.set_defaults(handler=_not_implemented)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

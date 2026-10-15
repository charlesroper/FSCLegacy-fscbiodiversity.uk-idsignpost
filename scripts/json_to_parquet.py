#!/usr/bin/env python3
"""Convert idsignpost.json to Apache Parquet format."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Canonical field order matching CSV and original schema
FIELD_ORDER = [
    "filename",
    "Title",
    "Submitted",
    "Description",
    "Author(s)",
    "Free",
    "Available as",
    "Web link",
    "Web link status",
    "Web link state",
    "Wayback link",
    "Alternative link",
    "Alternative probability",
    "Additional web link",
    "Keywords",
    "Comment Body",
]


def convert_entry_value(field: str, value: object, delimiter: str = ";") -> object:
    """Normalize a JSON field value for Parquet storage."""
    if value is None:
        return None

    if field == "Free":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            val_lower = value.strip().lower()
            if val_lower == "yes":
                return True
            if val_lower == "no":
                return False
        return None

    if field == "Alternative probability":
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    if isinstance(value, list):
        filtered = [str(v) for v in value if v]
        if not filtered:
            return None
        return delimiter.join(filtered)

    if isinstance(value, str):
        val_str = value.strip()
        return val_str if val_str else None

    return str(value)


def json_to_parquet(
    json_entries: list[dict], output_path: Path, array_delimiter: str = ";"
) -> None:
    """Convert JSON entries to a Parquet table with proper nulls and types."""
    # Determine all fields starting from canonical order
    all_fields = set()
    for entry in json_entries:
        all_fields.update(entry.keys())

    fieldnames = [f for f in FIELD_ORDER if f in all_fields]
    remaining = sorted(all_fields - set(fieldnames))
    fieldnames.extend(remaining)

    columns: dict[str, list] = {f: [] for f in fieldnames}
    for entry in json_entries:
        for f in fieldnames:
            raw_val = entry.get(f)
            val = convert_entry_value(f, raw_val, delimiter=array_delimiter)
            columns[f].append(val)

    # Build PyArrow schema and table
    arrow_fields = []
    arrow_arrays = []
    for f in fieldnames:
        if f == "Free":
            arrow_fields.append(pa.field(f, pa.bool_(), nullable=True))
            arrow_arrays.append(pa.array(columns[f], type=pa.bool_()))
        elif f == "Alternative probability":
            arrow_fields.append(pa.field(f, pa.float64(), nullable=True))
            arrow_arrays.append(pa.array(columns[f], type=pa.float64()))
        else:
            arrow_fields.append(pa.field(f, pa.string(), nullable=True))
            arrow_arrays.append(pa.array(columns[f], type=pa.string()))

    schema = pa.schema(arrow_fields)
    table = pa.Table.from_arrays(arrow_arrays, schema=schema)
    pq.write_table(table, output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert idsignpost.json to Apache Parquet format."
    )
    parser.add_argument(
        "--input",
        default="idsignpost.json",
        help="Input JSON file (default: idsignpost.json)",
    )
    parser.add_argument(
        "--output",
        default="idsignpost.parquet",
        help="Output Parquet file (default: idsignpost.parquet)",
    )
    parser.add_argument(
        "--delimiter",
        default=";",
        help="Delimiter for array fields (default: semicolon)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file '{input_path}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        entries = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(entries, list):
        print("Error: JSON root must be an array of entries.", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    json_to_parquet(entries, output_path, array_delimiter=args.delimiter)
    print(f"Wrote {len(entries)} entries to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()

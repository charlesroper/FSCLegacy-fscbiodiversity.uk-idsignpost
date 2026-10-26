"""Tests for the JSON, CSV, Parquet, and Markdown exporters."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from idsignpost.export import (
    COLUMNS,
    catalog_json,
    catalog_markdown,
    write_csv,
    write_json,
    write_markdown,
    write_parquet,
)
from idsignpost.models import CatalogEntry


def make_entry(entry_id: str, **overrides: object) -> CatalogEntry:
    data: dict[str, object] = {
        "id": entry_id,
        "filename": f"{entry_id}.html",
        "title": f"Title {entry_id}",
        "submitted_by": "Anonymous (not verified)",
        "submitted_at": "2018-05-16T13:26:00",
        "description": 'He said "hi", then left.\nSecond paragraph.',
        "authors": "Natural History Museum",
        "is_free": False,
        "available_as": ["PDF", "Online"],
        "keywords": "bats",
        "web_link": "https://example.org/guide.pdf",
        "additional_web_links": ["https://example.org/a", "https://example.org/b"],
        "web_link_status": "403",
        "web_link_state": "blocked",
        "wayback_link": "https://web.archive.org/web/2020/https://example.org/guide.pdf",
        "alternative_link": "https://example.org/new",
        "alternative_probability": 0.64,
        "alternative_reasoning": "Moved to a new page.",
        "verified_at": "2026-10-26T00:00:00+00:00",
    }
    data.update(overrides)
    return CatalogEntry.model_validate(data)


@pytest.fixture
def entries() -> list[CatalogEntry]:
    minimal = make_entry(
        "z-moth",
        title="Moths",
        submitted_by=None,
        submitted_at=None,
        description=None,
        authors=None,
        is_free=None,
        available_as=[],
        keywords=None,
        web_link=None,
        additional_web_links=[],
        web_link_status=None,
        web_link_state=None,
        wayback_link=None,
        alternative_link=None,
        alternative_probability=None,
        alternative_reasoning=None,
        verified_at=None,
    )
    return [make_entry("a-bats"), minimal]


def test_json_is_sorted_indented_and_unicode_friendly(
    entries: list[CatalogEntry],
) -> None:
    text = catalog_json(reversed(entries))

    assert [entry["id"] for entry in json.loads(text)] == ["a-bats", "z-moth"]
    assert text.endswith("\n")
    assert '\n  {\n    "id": "a-bats"' in text
    assert "\\u" not in catalog_json([make_entry("a-bats", title="Café")])


def test_csv_uses_canonical_columns_and_joins_lists(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    path = tmp_path / "idsignpost.csv"
    write_csv(entries, path)

    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    assert rows[0] == list(COLUMNS)

    header, *data = rows
    first = dict(zip(header, data[0], strict=True))
    second = dict(zip(header, data[1], strict=True))

    assert first["id"] == "a-bats"
    assert first["available_as"] == "PDF; Online"
    assert (
        first["additional_web_links"] == "https://example.org/a; https://example.org/b"
    )
    assert first["web_link_state"] == "unverified"
    assert second["web_link_state"] == ""
    assert second["alternative_probability"] == ""
    assert second["is_free"] == ""


def test_csv_quotes_embedded_newlines_and_quotes(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    path = tmp_path / "idsignpost.csv"
    write_csv(entries, path)

    raw = path.read_bytes()
    assert b'"He said ""hi"", then left.\nSecond paragraph."' in raw

    header, *data = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    first = dict(zip(header, data[0], strict=True))
    assert first["description"] == 'He said "hi", then left.\nSecond paragraph.'


def test_parquet_uses_native_types(entries: list[CatalogEntry], tmp_path: Path) -> None:
    path = tmp_path / "idsignpost.parquet"
    write_parquet(entries, path)

    table = pq.read_table(path)

    assert table.schema.field("is_free").type == "bool"
    available_as = table.schema.field("available_as").type
    assert pa.types.is_list(available_as)
    assert pa.types.is_string(available_as.value_type)
    assert table.schema.field("alternative_probability").type == "double"
    assert table.schema.field("title").type == "string"
    assert table.column("id").to_pylist() == ["a-bats", "z-moth"]

    first = table.slice(0, 1).to_pylist()[0]
    assert first["available_as"] == ["PDF", "Online"]
    assert first["is_free"] is False
    assert first["alternative_probability"] == pytest.approx(0.64)
    assert first["web_link_state"] == "unverified"

    second = table.slice(1, 1).to_pylist()[0]
    assert second["is_free"] is None
    assert second["alternative_probability"] is None
    assert second["available_as"] == []


def test_markdown_renders_sorted_entries_with_verification(
    entries: list[CatalogEntry],
) -> None:
    text = catalog_markdown(entries)

    assert text.startswith("## a-bats.html\n")
    assert text.index("## a-bats.html") < text.index("## z-moth.html")
    assert (
        "- Web link: [https://example.org/guide.pdf](<https://example.org/guide.pdf>)"
        in text
    )
    assert "- Web link state: unverified" in text
    assert "- Wayback link: [https://web.archive.org" in text
    assert (
        "- Alternative link: [https://example.org/new](<https://example.org/new>)"
        in text
    )
    assert "- Alternative probability: 0.64" in text
    assert "- Free: No" in text
    assert "- Available as:\n  - PDF\n  - Online" in text


def test_markdown_indents_multiline_values_by_two_spaces(
    entries: list[CatalogEntry],
) -> None:
    text = catalog_markdown([make_entry("a-bats")])

    assert '- Description:\n  He said "hi", then left.\n  Second paragraph.' in text


def test_markdown_omits_absent_optional_fields(
    entries: list[CatalogEntry],
) -> None:
    text = catalog_markdown([entry for entry in entries if entry.id == "z-moth"])

    assert "## z-moth.html" in text
    assert "Wayback link" not in text
    assert "Alternative link" not in text
    assert "Web link" not in text
    assert "Free" not in text


def test_writers_replace_existing_file_and_leave_no_temp(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    path = tmp_path / "idsignpost.json"
    path.write_text("stale", encoding="utf-8")

    write_json(entries, path)

    assert sorted(item.name for item in tmp_path.iterdir()) == ["idsignpost.json"]
    assert json.loads(path.read_text(encoding="utf-8"))[0]["id"] == "a-bats"


@pytest.mark.parametrize("suffix", ["json", "csv", "md"])
def test_text_exports_are_deterministic(
    suffix: str, entries: list[CatalogEntry], tmp_path: Path
) -> None:
    writer = {"json": write_json, "csv": write_csv, "md": write_markdown}[suffix]
    first = tmp_path / f"first.{suffix}"
    second = tmp_path / f"second.{suffix}"

    writer(entries, first)
    writer(list(reversed(entries)), second)

    assert first.read_bytes() == second.read_bytes()

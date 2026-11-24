"""Tests for the JSON, CSV, Parquet, and Markdown exporters."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from idsignpost.export import (
    COLUMNS,
    atomic_write,
    catalog_json,
    catalog_markdown,
    write_csv,
    write_json,
    write_markdown,
    write_parquet,
    write_sqlite,
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


def test_atomic_write_creates_missing_parent_directories(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    nested_json = tmp_path / "dist" / "sub" / "idsignpost.json"
    nested_bin = tmp_path / "other" / "deep" / "data.bin"

    write_json(entries, nested_json)
    atomic_write(nested_bin, lambda handle: handle.write(b"ok"), binary=True)

    assert [
        item["id"] for item in json.loads(nested_json.read_text(encoding="utf-8"))
    ] == ["a-bats", "z-moth"]
    assert nested_bin.read_bytes() == b"ok"


def test_sqlite_exports_normalized_tables_fts5_and_views(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    db_path = tmp_path / "db" / "idsignpost.db"
    broken_with_alt = make_entry(
        "c-beetles",
        title="Ground beetles guide",
        web_link="https://example.org/dead",
        web_link_status="404",
        web_link_state="broken",
        available_as=["Publication"],
        keywords="coleoptera carabidae",
        alternative_link="https://shop.example/carabidae",
        alternative_probability=0.9,
        alternative_note="confirmed: exact handbook page.",
        wayback_link="https://web.archive.org/web/2021/https://example.org/dead",
    )
    broken_unrecovered = make_entry(
        "d-flies",
        title="Hoverfly key",
        web_link="https://example.org/gone",
        web_link_status="404",
        web_link_state="broken",
        available_as=["Online"],
        alternative_link=None,
        alternative_probability=None,
        alternative_reasoning=None,
        alternative_note="no candidates returned",
    )
    live_entry = make_entry(
        "b-bees",
        title="Bumblebees field guide",
        is_free=True,
        web_link="https://bees.example/guide",
        web_link_status="200",
        web_link_state="ok",
        available_as=["Online", "PDF"],
        alternative_link=None,
        alternative_probability=None,
        alternative_reasoning=None,
    )

    write_sqlite([*entries, broken_with_alt, broken_unrecovered, live_entry], db_path)

    assert sorted(p.name for p in db_path.parent.iterdir()) == ["idsignpost.db"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == [
            "a-bats",
            "b-bees",
            "c-beetles",
            "d-flies",
            "z-moth",
        ]
        by_id = {row["id"]: row for row in rows}
        assert by_id["a-bats"]["web_link_state"] == "unverified"
        assert by_id["a-bats"]["is_free"] == 0
        assert by_id["b-bees"]["is_free"] == 1
        assert by_id["z-moth"]["is_free"] is None
        assert json.loads(by_id["a-bats"]["available_as"]) == ["PDF", "Online"]

        formats = conn.execute(
            "SELECT entry_id, format FROM entry_formats ORDER BY entry_id, format"
        ).fetchall()
        assert [(r["entry_id"], r["format"]) for r in formats] == [
            ("a-bats", "Online"),
            ("a-bats", "PDF"),
            ("b-bees", "Online"),
            ("b-bees", "PDF"),
            ("c-beetles", "Publication"),
            ("d-flies", "Online"),
        ]

        add_links = conn.execute(
            "SELECT entry_id, url FROM entry_additional_links ORDER BY entry_id, url"
        ).fetchall()
        assert ("a-bats", "https://example.org/a") in [
            (r["entry_id"], r["url"]) for r in add_links
        ]

        fts_hits = conn.execute(
            "SELECT id FROM entries_fts WHERE entries_fts MATCH 'carabidae'"
        ).fetchall()
        assert [r["id"] for r in fts_hits] == ["c-beetles"]

        working = conn.execute(
            "SELECT id, best_link, link_source, formats FROM working_guides ORDER BY id"
        ).fetchall()
        assert [
            (r["id"], r["best_link"], r["link_source"], r["formats"]) for r in working
        ] == [
            ("a-bats", "https://example.org/new", "alternative", "Online; PDF"),
            ("b-bees", "https://bees.example/guide", "original", "Online; PDF"),
            (
                "c-beetles",
                "https://shop.example/carabidae",
                "alternative",
                "Publication",
            ),
        ]

        unresolved = conn.execute(
            "SELECT id FROM broken_without_alternative ORDER BY id"
        ).fetchall()
        assert [r["id"] for r in unresolved] == ["d-flies"]

        recovered = conn.execute(
            "SELECT id, broken_web_link, alternative_link FROM recovered_alternatives ORDER BY id"
        ).fetchall()
        assert [
            (r["id"], r["broken_web_link"], r["alternative_link"]) for r in recovered
        ] == [
            ("c-beetles", "https://example.org/dead", "https://shop.example/carabidae")
        ]
    finally:
        conn.close()


def test_sqlite_replaces_existing_file_cleanly(
    entries: list[CatalogEntry], tmp_path: Path
) -> None:
    db_path = tmp_path / "idsignpost.db"
    db_path.write_text("stale", encoding="utf-8")

    write_sqlite(entries, db_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["idsignpost.db"]
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        assert count == 2
    finally:
        conn.close()

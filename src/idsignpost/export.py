"""Export the canonical catalog to JSON, CSV, Parquet, and Markdown."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from idsignpost.models import CatalogEntry

COLUMNS = tuple(CatalogEntry.model_fields)
LIST_FIELDS = frozenset({"available_as", "additional_web_links"})


def display_state(state: str | None) -> str | None:
    """Render a link state for human-facing exports."""
    return "unverified" if state == "blocked" else state


def _sorted(entries: Iterable[CatalogEntry]) -> list[CatalogEntry]:
    return sorted(entries, key=lambda entry: entry.id)


def _default_file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def atomic_write(
    path: str | Path,
    write: Callable[[IO[Any]], None],
    *,
    binary: bool = False,
) -> Path:
    """Write through a temporary file and atomically replace the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent or ".", prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        if binary:
            handle = os.fdopen(descriptor, "wb")
        else:
            handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        with handle:
            write(handle)
        os.chmod(temporary, _default_file_mode())
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def _write_text(path: str | Path, text: str) -> Path:
    return atomic_write(path, lambda handle: handle.write(text))


def catalog_json(entries: Iterable[CatalogEntry]) -> str:
    """Serialize entries as deterministic, human-readable JSON sorted by id."""
    payload = [entry.model_dump() for entry in _sorted(entries)]
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_json(entries: Iterable[CatalogEntry], path: str | Path) -> Path:
    """Write the canonical JSON catalog."""
    return _write_text(path, catalog_json(entries))


def _csv_cell(name: str, value: Any) -> str:
    if value is None:
        return ""
    if name == "web_link_state":
        return display_state(value) or ""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def write_csv(entries: Iterable[CatalogEntry], path: str | Path) -> Path:
    """Write a UTF-8 CSV with canonical snake_case columns."""

    def write(handle: IO[Any]) -> None:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for entry in _sorted(entries):
            writer.writerow(_csv_cell(name, getattr(entry, name)) for name in COLUMNS)

    return atomic_write(path, write)


def _arrow_type(name: str) -> Any:
    import pyarrow as pa

    if name == "is_free":
        return pa.bool_()
    if name in LIST_FIELDS:
        return pa.list_(pa.string())
    if name == "alternative_probability":
        return pa.float64()
    return pa.string()


def parquet_table(entries: Sequence[CatalogEntry]) -> Any:
    """Build the Arrow table with the native schema types."""
    import pyarrow as pa

    ordered = _sorted(entries)
    arrays = []
    for name in COLUMNS:
        values = []
        for entry in ordered:
            value = getattr(entry, name)
            if name == "web_link_state":
                value = display_state(value)
            values.append(value)
        arrays.append(pa.array(values, type=_arrow_type(name)))
    return pa.Table.from_arrays(arrays, names=list(COLUMNS))


def write_parquet(entries: Iterable[CatalogEntry], path: str | Path) -> Path:
    """Write an Apache Parquet table with native column types."""
    import pyarrow.parquet as pq

    table = parquet_table(list(entries))
    return atomic_write(path, lambda handle: pq.write_table(table, handle), binary=True)


def _markdown_link(url: str) -> str:
    return f"[{url}](<{url}>)"


def _bullet(lines: list[str], label: str, value: Any) -> None:
    if value is None or value == "":
        return
    text = str(value)
    if "\n" not in text:
        lines.append(f"- {label}: {text}")
        return
    lines.append(f"- {label}:")
    for line in text.splitlines():
        lines.append(f"  {line}" if line else "")


def _bullet_list(lines: list[str], label: str, values: Sequence[str]) -> None:
    if not values:
        return
    if len(values) == 1:
        _bullet(lines, label, values[0])
        return
    lines.append(f"- {label}:")
    lines.extend(f"  - {value}" for value in values)


def entry_markdown(entry: CatalogEntry) -> str:
    """Render one catalog entry as Markdown bullets."""
    lines = [f"## {entry.filename}", ""]
    _bullet(lines, "Title", entry.title)
    _bullet(
        lines,
        "Submitted",
        "; ".join(part for part in (entry.submitted_by, entry.submitted_at) if part),
    )
    _bullet(lines, "Description", entry.description)
    _bullet(lines, "Author(s)", entry.authors)
    if entry.is_free is not None:
        _bullet(lines, "Free", "Yes" if entry.is_free else "No")
    _bullet_list(lines, "Available as", entry.available_as)
    _bullet(lines, "Keywords", entry.keywords)
    _bullet(lines, "Comment Body", entry.comment_body)
    if entry.web_link:
        _bullet(lines, "Web link", _markdown_link(entry.web_link))
    _bullet_list(
        lines,
        "Additional web link",
        [_markdown_link(url) for url in entry.additional_web_links],
    )
    _bullet(lines, "Web link status", entry.web_link_status)
    _bullet(lines, "Web link state", display_state(entry.web_link_state))
    if entry.web_link_resolved:
        _bullet(lines, "Web link resolved", _markdown_link(entry.web_link_resolved))
    if entry.wayback_link:
        _bullet(lines, "Wayback link", _markdown_link(entry.wayback_link))
    if entry.alternative_link:
        _bullet(lines, "Alternative link", _markdown_link(entry.alternative_link))
    if entry.alternative_probability is not None:
        _bullet(
            lines, "Alternative probability", f"{entry.alternative_probability:.2f}"
        )
    _bullet(lines, "Alternative reasoning", entry.alternative_reasoning)
    _bullet(lines, "Alternative note", entry.alternative_note)
    _bullet(lines, "Verified at", entry.verified_at)
    return "\n".join(lines) + "\n"


def catalog_markdown(entries: Iterable[CatalogEntry]) -> str:
    """Render the whole catalog as Markdown, sorted by id."""
    sections = [entry_markdown(entry) for entry in _sorted(entries)]
    return "\n".join(sections)


def write_markdown(entries: Iterable[CatalogEntry], path: str | Path) -> Path:
    """Write the human-readable Markdown catalog."""
    return _write_text(path, catalog_markdown(entries))


_SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    submitted_by TEXT,
    submitted_at TEXT,
    description TEXT,
    authors TEXT,
    is_free INTEGER,
    available_as TEXT NOT NULL,
    keywords TEXT,
    comment_body TEXT,
    web_link TEXT,
    additional_web_links TEXT NOT NULL,
    web_link_status TEXT,
    web_link_state TEXT,
    web_link_resolved TEXT,
    wayback_link TEXT,
    alternative_link TEXT,
    alternative_probability REAL,
    alternative_reasoning TEXT,
    alternative_note TEXT,
    verified_at TEXT
);

CREATE TABLE entry_formats (
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    PRIMARY KEY (entry_id, format)
) WITHOUT ROWID;

CREATE TABLE entry_additional_links (
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    PRIMARY KEY (entry_id, url)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE entries_fts USING fts5(
    id UNINDEXED,
    title,
    authors,
    description,
    keywords,
    alternative_note,
    content='entries',
    content_rowid='rowid'
);

CREATE VIEW working_guides AS
SELECT
    e.id,
    e.title,
    e.authors,
    e.is_free,
    COALESCE(
        (
            SELECT group_concat(f.format, '; ')
            FROM (
                SELECT format
                FROM entry_formats
                WHERE entry_id = e.id
                ORDER BY format
            ) AS f
        ),
        ''
    ) AS formats,
    CASE
        WHEN e.web_link_state = 'ok' THEN e.web_link
        ELSE e.alternative_link
    END AS best_link,
    CASE
        WHEN e.web_link_state = 'ok' THEN 'original'
        ELSE 'alternative'
    END AS link_source,
    e.wayback_link,
    e.keywords,
    e.alternative_probability
FROM entries AS e
WHERE e.web_link_state = 'ok' OR e.alternative_link IS NOT NULL;

CREATE VIEW broken_without_alternative AS
SELECT
    id,
    title,
    authors,
    web_link,
    web_link_status,
    alternative_note,
    wayback_link,
    keywords
FROM entries
WHERE web_link_state = 'broken' AND alternative_link IS NULL;

CREATE VIEW recovered_alternatives AS
SELECT
    id,
    title,
    authors,
    web_link AS broken_web_link,
    web_link_status,
    alternative_link,
    alternative_probability,
    alternative_reasoning,
    alternative_note,
    wayback_link
FROM entries
WHERE web_link_state = 'broken' AND alternative_link IS NOT NULL;
"""


def _sqlite_row(entry: CatalogEntry) -> tuple[Any, ...]:
    values: list[Any] = []
    for name in COLUMNS:
        value = getattr(entry, name)
        if name == "web_link_state":
            value = display_state(value)
        elif name == "is_free":
            value = None if value is None else int(bool(value))
        elif name in LIST_FIELDS:
            value = json.dumps(value, ensure_ascii=False)
        values.append(value)
    return tuple(values)


def write_sqlite(entries: Iterable[CatalogEntry], path: str | Path) -> Path:
    """Write a normalised, FTS5-indexed SQLite catalog for Datasette."""
    ordered = _sorted(entries)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent or ".", prefix=f".{target.name}.", suffix=".db.tmp"
    )
    os.close(descriptor)
    placeholders = ", ".join("?" for _ in COLUMNS)
    columns_sql = ", ".join(COLUMNS)
    try:
        connection = sqlite3.connect(temporary)
        try:
            with connection:
                connection.executescript(_SQLITE_SCHEMA)
                connection.executemany(
                    f"INSERT INTO entries ({columns_sql}) VALUES ({placeholders})",
                    [_sqlite_row(entry) for entry in ordered],
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO entry_formats (entry_id, format) VALUES (?, ?)",
                    [
                        (entry.id, fmt)
                        for entry in ordered
                        for fmt in sorted(set(entry.available_as))
                    ],
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO entry_additional_links (entry_id, url) VALUES (?, ?)",
                    [
                        (entry.id, url)
                        for entry in ordered
                        for url in sorted(set(entry.additional_web_links))
                    ],
                )
                connection.execute(
                    "INSERT INTO entries_fts(entries_fts) VALUES('rebuild')"
                )
        finally:
            connection.close()
        os.chmod(temporary, _default_file_mode())
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target

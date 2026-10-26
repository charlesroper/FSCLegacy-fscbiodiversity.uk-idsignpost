"""Tests for canonical catalog models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from idsignpost.models import (
    CANONICAL_STATUS_ERRORS,
    CatalogEntry,
    ensure_unique_ids,
    load_catalog,
)


def make_entry(entry_id: str = "bats-flight", **overrides: object) -> CatalogEntry:
    data: dict[str, object] = {
        "id": entry_id,
        "filename": f"{entry_id}.html",
        "title": "Bats in flight",
    }
    data.update(overrides)
    return CatalogEntry.model_validate(data)


def test_defaults_are_empty_not_shared() -> None:
    first = make_entry()
    second = make_entry("moths")

    first.available_as.append("PDF")
    first.additional_web_links.append("https://example.org/a")

    assert second.available_as == []
    assert second.additional_web_links == []
    assert second.submitted_by is None
    assert second.web_link_state is None


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_entry(filename_typo="bats-flight.html")


@pytest.mark.parametrize("value", [0.0, 0.4, 1.0])
def test_probability_accepts_bounds(value: float) -> None:
    assert make_entry(alternative_probability=value).alternative_probability == value


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_probability_rejects_out_of_range(value: float) -> None:
    with pytest.raises(ValidationError):
        make_entry(alternative_probability=value)


@pytest.mark.parametrize("value", ["200", "404", "503", *CANONICAL_STATUS_ERRORS])
def test_status_accepts_canonical_values(value: str) -> None:
    assert make_entry(web_link_status=value).web_link_status == value


@pytest.mark.parametrize("value", ["2xx", "99", "600", "OK", "dead", ""])
def test_status_rejects_unknown_values(value: str) -> None:
    with pytest.raises(ValidationError):
        make_entry(web_link_status=value)


def test_submitted_at_must_be_naive() -> None:
    entry = make_entry(submitted_at="2024-05-01T09:30:00")
    assert entry.submitted_at == "2024-05-01T09:30:00"

    with pytest.raises(ValidationError):
        make_entry(submitted_at="2024-05-01T09:30:00+01:00")


def test_verified_at_must_be_utc() -> None:
    assert make_entry(verified_at="2026-10-26T12:00:00Z").verified_at is not None
    assert make_entry(verified_at="2026-10-26T12:00:00+00:00").verified_at is not None

    with pytest.raises(ValidationError):
        make_entry(verified_at="2026-10-26T12:00:00")
    with pytest.raises(ValidationError):
        make_entry(verified_at="2026-10-26T12:00:00+02:00")


def test_ensure_unique_ids_reports_duplicates() -> None:
    entries = [make_entry("bats"), make_entry("moths"), make_entry("bats")]

    with pytest.raises(ValueError, match="bats"):
        ensure_unique_ids(entries)


def test_load_catalog_round_trips_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "idsignpost.json"
    path.write_text(
        json.dumps([make_entry().model_dump(), make_entry("moths").model_dump()]),
        encoding="utf-8",
    )

    entries = load_catalog(path)

    assert [entry.id for entry in entries] == ["bats-flight", "moths"]


def test_load_catalog_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "idsignpost.json"
    path.write_text(
        json.dumps([make_entry().model_dump(), make_entry().model_dump()]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate catalog ids"):
        load_catalog(path)


def test_load_catalog_rejects_non_array(tmp_path: Path) -> None:
    path = tmp_path / "idsignpost.json"
    path.write_text(json.dumps({"entries": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="JSON array"):
        load_catalog(path)

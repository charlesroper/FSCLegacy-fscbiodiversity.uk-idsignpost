"""Tests for canonical catalog models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from idsignpost.models import (
    CANONICAL_STATUS_ERRORS,
    VERIFICATION_FIELDS,
    CatalogEntry,
    ensure_unique_ids,
    load_catalog,
    merge_verification,
    merge_verified,
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


def test_merge_verification_copies_without_mutating_inputs() -> None:
    fresh = make_entry("bats", web_link="https://example.org/bats")
    previous = make_entry(
        "bats",
        web_link="https://example.org/bats",
        web_link_status="200",
        web_link_state="ok",
        web_link_resolved="https://example.org/bats-new",
        wayback_link="https://web.archive.org/web/2020/https://example.org/bats",
        alternative_link="https://example.org/alt",
        alternative_probability=0.9,
        alternative_reasoning="Official guide.",
        alternative_note="confirmed: official guide",
        verified_at="2026-01-01T00:00:00+00:00",
    )

    merged = merge_verification([fresh], [previous])

    assert len(merged) == 1
    assert merged[0] is not fresh
    assert fresh.web_link_state is None
    assert fresh.web_link_status is None
    for field in VERIFICATION_FIELDS:
        assert getattr(merged[0], field) == getattr(previous, field)


def test_merge_verification_clears_all_fields_when_web_link_changes() -> None:
    fresh = make_entry(
        "bats",
        web_link="https://example.org/new",
        web_link_status="200",
        web_link_state="ok",
        web_link_resolved="https://example.org/old-resolved",
        wayback_link="https://web.archive.org/web/2020/https://example.org/old",
        alternative_link="https://example.org/alt",
        alternative_probability=0.8,
        alternative_reasoning="Old reason.",
        alternative_note="confirmed: old",
        verified_at="2026-01-01T00:00:00+00:00",
    )
    previous = make_entry(
        "bats",
        web_link="https://example.org/old",
        web_link_status="404",
        web_link_state="broken",
    )

    merged = merge_verification([fresh], [previous])

    assert merged[0] is not fresh
    assert fresh.web_link_state == "ok"
    for field in VERIFICATION_FIELDS:
        assert getattr(merged[0], field) is None


def test_merge_verified_updates_only_verified_entries_immutably() -> None:
    catalog = [
        make_entry("a", web_link="https://example.org/a"),
        make_entry("b", web_link="https://example.org/b"),
        make_entry("c", web_link="https://example.org/c", web_link_state="broken"),
    ]
    verified = [
        make_entry(
            "b",
            web_link="https://example.org/b",
            web_link_status="200",
            web_link_state="ok",
            verified_at="2026-01-01T00:00:00+00:00",
        )
    ]

    merged = merge_verified(catalog, verified)

    assert [item.web_link_state for item in merged] == [None, "ok", "broken"]
    assert merged[1] is not catalog[1]
    assert merged[1] is not verified[0]
    assert catalog[1].web_link_state is None
    assert merged[1].web_link_status == "200"


def test_merge_verified_clears_fields_when_link_changed() -> None:
    catalog = [
        make_entry(
            "a",
            web_link="https://example.org/new",
            web_link_state="ok",
            web_link_status="200",
            wayback_link="https://web.archive.org/web/2020/https://example.org/old",
            verified_at="2026-01-01T00:00:00+00:00",
        )
    ]
    verified = [
        make_entry(
            "a",
            web_link="https://example.org/old",
            web_link_state="broken",
            web_link_status="404",
        )
    ]

    merged = merge_verified(catalog, verified)

    assert merged[0] is not catalog[0]
    assert catalog[0].web_link_state == "ok"
    for field in VERIFICATION_FIELDS:
        assert getattr(merged[0], field) is None


def test_merge_verified_rejects_unknown_and_duplicate_ids() -> None:
    catalog = [make_entry("a")]

    with pytest.raises(ValueError, match="not in catalog"):
        merge_verified(catalog, [make_entry("missing")])

    with pytest.raises(ValueError, match="duplicate"):
        merge_verified(catalog, [make_entry("a"), make_entry("a")])


def test_repository_prose_and_code_contain_no_em_dashes() -> None:
    root = Path(__file__).resolve().parent.parent
    candidates = [
        *root.glob("*.md"),
        *root.glob("src/**/*.py"),
        *root.glob("tests/**/*.py"),
        *root.glob("docs/**/*.md"),
        *root.glob("plans/**/*.md"),
        root / "datasette.metadata.json",
        *root.glob(".github/workflows/*.yml"),
        *root.glob("src/idsignpost/web/*.html"),
        *root.glob("src/idsignpost/web/*.css"),
        *root.glob("src/idsignpost/web/*.js"),
        *root.glob("tests/js/*.mjs"),
    ]
    offenders = [
        str(path.relative_to(root))
        for path in sorted(set(candidates))
        if path.is_file()
        and path.name != "idsignpost.md"
        and "\u2014" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

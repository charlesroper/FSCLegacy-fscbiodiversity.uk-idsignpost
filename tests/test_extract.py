"""Tests for direct HTML to CatalogEntry extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from idsignpost.export import catalog_json
from idsignpost.extract import extract_catalog, merge_verification, parse_entry, slugify
from idsignpost.models import CatalogEntry, load_catalog

FIXTURES = Path(__file__).parent / "fixtures"
FULL_ENTRY = (FIXTURES / "full-entry.html").read_text(encoding="utf-8")
FLICKR = (FIXTURES / "flickr-split.html").read_text(encoding="utf-8")


def test_extracts_identity_and_metadata() -> None:
    entry = parse_entry(FULL_ENTRY, "full-entry.html")

    assert entry.id == "full-entry"
    assert entry.filename == "full-entry.html"
    assert entry.title == "Café bats"
    assert entry.submitted_by == "Anonymous (not verified)"
    assert entry.submitted_at == "2018-05-16T13:26:00"
    assert entry.authors == "Natural History Museum"
    assert entry.keywords == "bats chiroptera"


def test_decodes_entities_twice_and_normalizes_nbsp() -> None:
    description = parse_entry(FULL_ENTRY, "full-entry.html").description

    assert description is not None
    assert "Telling bats apart\nis hard & useful." in description
    assert "\u00a0" not in description


def test_description_links_use_sanitized_angle_brackets() -> None:
    description = parse_entry(FULL_ENTRY, "full-entry.html").description

    assert description is not None
    assert (
        "[guide](<https://www.fscbiodiversity.uk/resources/guide.pdf>)" in description
    )
    assert "utm_source" not in description
    assert "fbclid" not in description


def test_normalizes_boolean_and_list_fields() -> None:
    entry = parse_entry(FULL_ENTRY, "full-entry.html")

    assert entry.is_free is True
    assert entry.available_as == ["PDF", "Online"]


def test_ignores_fields_outside_main() -> None:
    # The sidebar holds a "Free: No" field that must not override the page.
    assert parse_entry(FULL_ENTRY, "full-entry.html").is_free is True


def test_extracts_relative_web_link_without_tracking() -> None:
    entry = parse_entry(FULL_ENTRY, "full-entry.html")

    assert entry.web_link == "https://www.fscbiodiversity.uk/resources/guide.pdf"


def test_deduplicates_additional_web_links() -> None:
    entry = parse_entry(FULL_ENTRY, "full-entry.html")

    assert entry.additional_web_links == [
        "https://example.org/more",
        "https://example.org/other",
    ]


def test_extracts_comment_body_as_markdown() -> None:
    entry = parse_entry(FULL_ENTRY, "full-entry.html")

    assert (
        entry.comment_body
        == "This tool moved to [a new home](<https://example.org/new>)."
    )


def test_repairs_split_flickr_url() -> None:
    entry = parse_entry(FLICKR, "flickr-split.html")

    assert entry.web_link == "https://www.flickr.com/photos/u/sets/721@N07/123"
    assert entry.submitted_by == "Jane Doe"
    assert entry.submitted_at == "2019-02-01T09:05:00"
    assert entry.is_free is None
    assert entry.available_as == []


def test_missing_main_is_an_error() -> None:
    with pytest.raises(ValueError, match='no <div id="main">'):
        parse_entry("<html><body>nothing</body></html>", "empty.html")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("bats-flight.html", "bats-flight"),
        ("Café_Über.html", "cafe-uber"),
        ("  Already--Dashed.HTML", "already-dashed"),
        ("dir/nested name.html", "nested-name"),
    ],
)
def test_slugify(filename: str, expected: str) -> None:
    assert slugify(filename) == expected


def test_slugify_derives_id_from_unicode_filename() -> None:
    entry = parse_entry(FLICKR, "Café Bats.html")

    assert entry.id == "cafe-bats"


def test_extract_catalog_rejects_duplicate_ids(tmp_path: Path) -> None:
    (tmp_path / "cafe.html").write_text(FLICKR, encoding="utf-8")
    (tmp_path / "café.html").write_text(FLICKR, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate catalog ids: cafe"):
        extract_catalog(tmp_path)


def test_extract_catalog_skips_about_and_honours_limit(tmp_path: Path) -> None:
    (tmp_path / "about.html").write_text(FLICKR, encoding="utf-8")
    (tmp_path / "a.html").write_text(FLICKR, encoding="utf-8")
    (tmp_path / "b.html").write_text(FLICKR, encoding="utf-8")

    assert [entry.id for entry in extract_catalog(tmp_path)] == ["a", "b"]
    assert [entry.id for entry in extract_catalog(tmp_path, limit=1)] == ["a"]


def make_verified(web_link: str | None = "https://example.org/a") -> CatalogEntry:
    return CatalogEntry.model_validate(
        {
            "id": "full-entry",
            "filename": "full-entry.html",
            "title": "Café bats",
            "web_link": web_link,
            "web_link_status": "200",
            "web_link_state": "ok",
            "web_link_resolved": "https://example.org/b",
            "wayback_link": "https://web.archive.org/web/2020/https://example.org/a",
            "alternative_link": "https://example.org/alt",
            "alternative_probability": 0.85,
            "alternative_reasoning": "Moved to new host.",
            "alternative_note": "confirmed: official guide page",
            "verified_at": "2026-01-01T00:00:00+00:00",
        }
    )


def test_merge_verification_preserves_unchanged_link() -> None:
    fresh = parse_entry(FULL_ENTRY, "full-entry.html")
    old = make_verified(fresh.web_link)

    merged = merge_verification([fresh], [old])[0]

    assert merged.web_link_status == "200"
    assert merged.web_link_state == "ok"
    assert merged.wayback_link == old.wayback_link
    assert merged.alternative_link == "https://example.org/alt"
    assert merged.alternative_probability == 0.85
    assert merged.alternative_reasoning == "Moved to new host."
    assert merged.alternative_note == "confirmed: official guide page"
    assert merged.verified_at == "2026-01-01T00:00:00+00:00"


def test_merge_verification_clears_when_link_changes() -> None:
    fresh = parse_entry(FULL_ENTRY, "full-entry.html")
    old = make_verified("https://example.org/old")

    merged = merge_verification([fresh], [old])[0]

    assert merged.web_link_status is None
    assert merged.web_link_state is None
    assert merged.wayback_link is None
    assert merged.alternative_link is None
    assert merged.alternative_note is None
    assert merged.verified_at is None


def test_merge_verification_ignores_unknown_ids() -> None:
    fresh = parse_entry(FULL_ENTRY, "full-entry.html")

    assert merge_verification([fresh], [])[0].web_link_state is None


def test_catalog_json_round_trips(tmp_path: Path) -> None:
    entries = [parse_entry(FULL_ENTRY, "full-entry.html")]
    path = tmp_path / "idsignpost.json"
    path.write_text(catalog_json(entries), encoding="utf-8")

    assert load_catalog(path) == entries
    assert catalog_json(entries).endswith("\n")

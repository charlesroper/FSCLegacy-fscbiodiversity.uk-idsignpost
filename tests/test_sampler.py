"""Tests for stratified cross-section sampling."""

from __future__ import annotations

import pytest

from idsignpost.models import CatalogEntry
from idsignpost.sampler import get_stratified_sample


def make_entry(
    entry_id: str, state: str | None = None, **overrides: object
) -> CatalogEntry:
    data: dict[str, object] = {
        "id": entry_id,
        "filename": f"{entry_id}.html",
        "title": f"Guide {entry_id}",
        "web_link": f"https://example.org/{entry_id}",
        "web_link_state": state,
    }
    data.update(overrides)
    return CatalogEntry.model_validate(data)


def ids(entries: list[CatalogEntry]) -> list[str]:
    return [entry.id for entry in entries]


def test_excludes_entries_without_web_link() -> None:
    entries = [make_entry("a", "ok"), make_entry("b", web_link=None)]

    assert ids(get_stratified_sample(entries, 10)) == ["a"]


@pytest.mark.parametrize("size", [0, -1])
def test_rejects_non_positive_size(size: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        get_stratified_sample([make_entry("a", "ok")], size)


def test_clamps_to_eligible_count() -> None:
    entries = [make_entry("a", "ok"), make_entry("b", web_link=None)]

    assert ids(get_stratified_sample(entries, 50)) == ["a"]


def test_returns_empty_when_nothing_is_eligible() -> None:
    assert get_stratified_sample([make_entry("a", web_link=None)], 5) == []


def test_balances_quota_across_states() -> None:
    entries = [
        *(make_entry(f"ok-{index}", "ok") for index in range(10)),
        *(make_entry(f"broken-{index}", "broken") for index in range(10)),
        *(make_entry(f"blocked-{index}", "blocked") for index in range(10)),
    ]

    sample = get_stratified_sample(entries, 9)

    assert len(sample) == 9
    for state in ("ok", "broken", "blocked"):
        assert sum(1 for entry in sample if entry.web_link_state == state) == 3


def test_redistributes_shortfall_to_larger_buckets() -> None:
    entries = [
        make_entry("ok-only", "ok"),
        *(make_entry(f"broken-{index}", "broken") for index in range(10)),
        *(make_entry(f"blocked-{index}", "blocked") for index in range(10)),
    ]

    sample = get_stratified_sample(entries, 9)

    assert sum(1 for entry in sample if entry.web_link_state == "ok") == 1
    assert sum(1 for entry in sample if entry.web_link_state == "broken") == 4
    assert sum(1 for entry in sample if entry.web_link_state == "blocked") == 4


def test_unverified_entries_form_their_own_bucket() -> None:
    entries = [
        make_entry("fresh-a"),
        make_entry("fresh-b"),
        make_entry("live", "ok"),
    ]

    sample = get_stratified_sample(entries, 2)

    assert ids(sample) == ["fresh-a", "live"]


def test_same_seed_reproduces_the_same_sample() -> None:
    entries = [make_entry(f"entry-{index}", "ok") for index in range(50)]

    first = get_stratified_sample(entries, 8, seed=7)
    second = get_stratified_sample(list(reversed(entries)), 8, seed=7)

    assert ids(first) == ids(second)


def test_different_seeds_draw_different_entries() -> None:
    entries = [make_entry(f"entry-{index:02d}", "ok") for index in range(50)]

    first = ids(get_stratified_sample(entries, 8, seed=1))
    second = ids(get_stratified_sample(entries, 8, seed=2))

    assert first != second


def test_output_is_sorted_by_id_and_unique() -> None:
    entries = [
        *(make_entry(f"ok-{index:02d}", "ok") for index in range(5)),
        *(make_entry(f"broken-{index:02d}", "broken") for index in range(5)),
    ]

    sample = get_stratified_sample(entries, 6)

    assert ids(sample) == sorted(ids(sample))
    assert len(ids(sample)) == len(set(ids(sample)))


def test_full_sample_is_sorted_by_id() -> None:
    entries = [
        make_entry("z-online", "ok", available_as=["Online"], keywords="b"),
        make_entry("a-pdf", "ok", available_as=["PDF"], keywords="z"),
        make_entry("m-pdf", "ok", available_as=["PDF"], keywords="a"),
    ]

    assert get_stratified_sample(entries, 3, seed=1) == sorted(
        entries, key=lambda entry: entry.id
    )

"""Deterministic stratified sampling for partial verification runs."""

from __future__ import annotations

import random

from idsignpost.models import CatalogEntry

BUCKET_ORDER = ("ok", "broken", "blocked", "unverified")


def _bucket(entry: CatalogEntry) -> str:
    state = entry.web_link_state
    return state if state in {"ok", "broken", "blocked"} else "unverified"


def _allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Split a quota across buckets, redistributing any shortfall."""
    quotas = dict.fromkeys(sizes, 0)
    active = [name for name in BUCKET_ORDER if sizes.get(name, 0) > 0]
    remaining = min(total, sum(sizes.values()))

    while remaining > 0 and active:
        share = remaining // len(active)
        if share == 0:
            for name in active[:remaining]:
                quotas[name] += 1
            break
        for name in list(active):
            give = min(share, sizes[name] - quotas[name])
            quotas[name] += give
            remaining -= give
            if quotas[name] >= sizes[name]:
                active.remove(name)
    return quotas


def get_stratified_sample(
    entries: list[CatalogEntry], sample_size: int, seed: int = 42
) -> list[CatalogEntry]:
    """Draw a reproducible sample balanced across link states."""
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")

    buckets: dict[str, list[CatalogEntry]] = {name: [] for name in BUCKET_ORDER}
    for entry in entries:
        if entry.web_link:
            buckets[_bucket(entry)].append(entry)

    for name, members in buckets.items():
        members.sort(
            key=lambda entry: (
                tuple(entry.available_as),
                entry.keywords or "",
                entry.id,
            )
        )

    quotas = _allocate(
        {name: len(members) for name, members in buckets.items()}, sample_size
    )
    rng = random.Random(seed)
    chosen: list[CatalogEntry] = []
    for name in BUCKET_ORDER:
        quota = quotas[name]
        if quota:
            chosen.extend(rng.sample(buckets[name], quota))
    return sorted(chosen, key=lambda entry: entry.id)

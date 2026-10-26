"""Live smoke tests, run with `uv run pytest -m live`."""

from __future__ import annotations

from pathlib import Path

import pytest

from idsignpost.extract import extract_catalog
from idsignpost.http_checker import HTTPChecker
from idsignpost.sampler import get_stratified_sample
from idsignpost.wayback import WaybackClient

HTML_DIR = Path(__file__).parent.parent / "idsignpost"


@pytest.mark.live
async def test_sampled_links_answer_http_and_wayback() -> None:
    if not HTML_DIR.is_dir():
        pytest.skip("legacy HTML corpus is not present")

    entries = extract_catalog(HTML_DIR)
    sample = get_stratified_sample(entries, 3)
    assert sample, "the corpus must yield verifiable entries"

    checker = HTTPChecker(timeout=20)
    wayback = WaybackClient(timeout=20)
    try:
        for entry in sample:
            assert entry.web_link is not None
            result = await checker.check(entry.web_link, entry_title=entry.title)
            assert result.state in {"ok", "broken", "blocked"}
        snapshot = await wayback.lookup(sample[0].web_link or "")
    finally:
        await checker.aclose()
        await wayback.aclose()

    assert snapshot is None or snapshot.startswith("https://web.archive.org/")

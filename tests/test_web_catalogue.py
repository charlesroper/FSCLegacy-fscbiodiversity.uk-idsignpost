"""Seam 2: run the Node tests for the catalogue and theme logic."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

JS_TESTS = Path(__file__).resolve().parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_catalogue_logic_passes_under_node() -> None:
    result = subprocess.run(
        ["node", "--test", *map(str, sorted(JS_TESTS.glob("*.test.mjs")))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

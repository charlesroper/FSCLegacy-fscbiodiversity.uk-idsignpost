"""Seam 2: run Node syntax checks, unit tests and optional headless layout checks."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "src" / "idsignpost" / "web"
JS_TESTS = REPO_ROOT / "tests" / "js"
PLAYWRIGHT_CORE = WEB_DIR / "node_modules" / "playwright-core"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_web_js_syntax_and_catalogue_logic_pass_under_node() -> None:
    for js_file in sorted(WEB_DIR.glob("*.js")):
        syntax = subprocess.run(
            ["node", "--check", str(js_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        assert syntax.returncode == 0, syntax.stdout + syntax.stderr

    result = subprocess.run(
        ["node", "--test", *map(str, sorted(JS_TESTS.glob("*.test.mjs")))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    shutil.which("node") is None or not PLAYWRIGHT_CORE.exists(),
    reason="Node.js or playwright-core is not installed",
)
def test_web_layout_passes_in_headless_browser() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "check_layout.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if result.returncode == 2:
        pytest.skip(result.stderr.strip() or "No Chromium binary available")
    assert result.returncode == 0, result.stdout + result.stderr

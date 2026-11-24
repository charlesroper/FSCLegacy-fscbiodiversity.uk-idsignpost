"""Tests for the static mobile web explorer assets and deployment workflow."""

from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import unquote

from idsignpost.models import CatalogEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "src" / "idsignpost" / "web"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pages.yml"


def _duckdb_extraswaps(value: str) -> str:
    swaps = {" ": "-", "-": " ", ";": "~", "~": ";"}
    return "".join(swaps.get(ch, ch) for ch in value)


def test_web_assets_exist_and_contain_no_em_dashes() -> None:
    expected = ("index.html", "styles.css", "app.js")
    for filename in expected:
        path = WEB_DIR / filename
        assert path.is_file(), f"Missing {path}"
        content = path.read_text(encoding="utf-8")
        assert "\u2014" not in content, f"Unexpected em-dash in {filename}"


def test_app_js_entry_fields_match_catalog_entry_schema() -> None:
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bentry\.([a-z][a-z0-9_]*)\b", app_js))
    # Ignore private client-side cache property attached in init().
    referenced.discard("_search")
    assert referenced, "Expected app.js to reference CatalogEntry fields"
    unknown = referenced - set(CatalogEntry.model_fields)
    assert not unknown, f"Unknown CatalogEntry fields in app.js: {sorted(unknown)}"


def test_index_html_has_expected_ids_and_valid_duckdb_shell_link() -> None:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # Every document.getElementById(...) lookup in app.js must match an element id in index.html.
    for element_id in set(re.findall(r'getElementById\("([^"]+)"\)', app_js)):
        assert f'id="{element_id}"' in html, f"Missing id={element_id!r} in index.html"

    # External guide links opened in new tabs must enforce noopener noreferrer.
    assert "noopener noreferrer" in app_js

    # Verify DuckDB Web Shell query hash decodes cleanly to our repository's Parquet URL.
    match = re.search(r'id="duckdb-link"\s+href="([^"]+)"', html)
    assert match is not None
    href = match.group(1)
    assert href.startswith("https://shell.duckdb.org/#queries=v0,")
    raw_queries = href.split("#queries=v0,", 1)[1].split(",")
    decoded_sql = "\n".join(_duckdb_extraswaps(unquote(part)) for part in raw_queries)
    assert (
        "https://raw.githubusercontent.com/charlesroper/"
        "FSCLegacy-fscbiodiversity.uk-idsignpost/main/idsignpost.parquet"
    ) in decoded_sql


def test_pages_workflow_packages_web_assets_and_catalog_files() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for required in (
        "src/idsignpost/web/index.html",
        "src/idsignpost/web/styles.css",
        "src/idsignpost/web/app.js",
        "idsignpost.json",
        "idsignpost.csv",
        "idsignpost.parquet",
        "idsignpost.db",
        "actions/deploy-pages@v5",
    ):
        assert required in workflow

    # Sanity-check that idsignpost.json is valid and non-empty if present in repo root.
    catalog_path = REPO_ROOT / "idsignpost.json"
    if catalog_path.is_file():
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        assert isinstance(data, list) and len(data) == 736

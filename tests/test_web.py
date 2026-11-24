"""Seam 3: the markup contract for the static catalogue in src/idsignpost/web/."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote

import pytest

from idsignpost.models import CatalogEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "src" / "idsignpost" / "web"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "pages.yml"
SHIPPED = ("index.html", "styles.css", "app.js", "catalogue.js", "theme.js")
REMOVED_CHROME = (
    "breadcrumb",
    "Apply filters",
    "Audited",
    "Public Index",
    "phase-banner",
    "palette",
)
AVOIDED_TERMS = ("Recovered link", "replacement link", "fallback", "Open replacement")


def _read(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def _duckdb_extraswaps(value: str) -> str:
    swaps = {" ": "-", "-": " ", ";": "~", "~": ";"}
    return "".join(swaps.get(ch, ch) for ch in value)


def _top_level_statements(css: str) -> list[str]:
    """Return the first token of each depth-0 statement, ignoring comments."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    statements, depth, current = [], 0, ""
    for char in css:
        if char == "{":
            if depth == 0:
                statements.append(current.strip())
                current = ""
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ";" and depth == 0:
            statements.append(current.strip())
            current = ""
        elif depth == 0:
            current += char
    return statements


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_files_exist(name: str) -> None:
    assert (WEB_DIR / name).is_file()


def test_stylesheet_orders_layers_and_keeps_govuk_in_its_own_layer() -> None:
    css = _read("styles.css")
    statements = _top_level_statements(css)
    assert statements[0] == "@layer govuk, base, components, theme"
    assert any(
        s.startswith("@import")
        and "govuk-frontend@5.7.1" in s
        and s.endswith("layer(govuk)")
        for s in statements
    )


def test_stylesheet_keeps_every_rule_inside_a_layer() -> None:
    statements = _top_level_statements(_read("styles.css"))
    loose = [s for s in statements if not s.startswith(("@layer", "@import"))]
    assert loose == []


def test_stylesheet_never_uses_important() -> None:
    assert "!important" not in _read("styles.css")


def test_theme_script_runs_before_any_stylesheet_to_avoid_a_flash() -> None:
    html = _read("index.html")
    script = html.index("localStorage")
    assert script < html.index('rel="stylesheet"')
    assert "data-theme" in html[: html.index('rel="stylesheet"')]
    assert 'name="color-scheme"' in html


def test_page_loads_govuk_and_alegreya_with_a_preload() -> None:
    html = _read("index.html")
    assert 'rel="preload"' in html and "govuk-frontend@5.7.1" in html
    assert "fonts.googleapis.com/css2?family=Alegreya+Sans" in html
    assert 'href="styles.css"' in html


def test_page_brand_marks_revived_in_italics() -> None:
    html = _read("index.html")
    assert "<title>ID Resource Signpost Revived" in html
    assert re.search(r"ID Resource Signpost\s*<em[^>]*>Revived</em>", html)


@pytest.mark.parametrize("fragment", REMOVED_CHROME)
def test_removed_chrome_stays_removed(fragment: str) -> None:
    combined = (_read("index.html") + _read("app.js") + _read("styles.css")).lower()
    assert fragment.lower() not in combined


@pytest.mark.parametrize("term", AVOIDED_TERMS)
def test_copy_uses_glossary_terms(term: str) -> None:
    combined = _read("index.html") + _read("app.js") + _read("catalogue.js")
    assert term.lower() not in combined.lower()


def test_every_element_the_script_looks_up_exists() -> None:
    html = _read("index.html")
    script = _read("app.js")
    ids = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    assert ids
    for element_id in ids:
        assert f'id="{element_id}"' in html, f"Missing id={element_id!r} in index.html"


def test_filter_toggle_is_a_disclosure_button_pointing_at_the_panel() -> None:
    html = _read("index.html")
    button = re.search(r'<button[^>]*id="filter-toggle"[^>]*>', html)
    assert button is not None
    tag = button.group(0)
    assert 'aria-expanded="false"' in tag
    controls = re.search(r'aria-controls="([^"]+)"', tag)
    assert controls is not None and f'id="{controls.group(1)}"' in html


def test_status_help_uses_the_govuk_details_component() -> None:
    html = _read("index.html")
    assert re.search(r'<details class="govuk-details"[^>]*>', html)
    assert "What the link statuses mean" in html


def test_page_has_skip_link_and_landmarks() -> None:
    html = _read("index.html")
    assert 'class="govuk-skip-link"' in html
    for landmark in ("<header", "<main", "<footer"):
        assert landmark in html


def test_script_reads_only_catalog_entry_fields() -> None:
    names: set[str] = set()
    for name in ("app.js", "catalogue.js"):
        names |= set(re.findall(r"\bentry\.([a-z][a-z0-9_]*)\b", _read(name)))
    assert names
    unknown = names - set(CatalogEntry.model_fields)
    assert not unknown, f"Unknown CatalogEntry fields: {sorted(unknown)}"


def test_script_builds_markup_with_the_dom_not_innerhtml() -> None:
    assert "innerHTML" not in _read("app.js")


def test_external_links_open_with_noopener_noreferrer() -> None:
    script = _read("app.js")
    assert 'target = "_blank"' in script
    assert "noopener noreferrer" in script


def test_footer_links_to_downloads_and_a_working_duckdb_query() -> None:
    html = _read("index.html")
    for download in (
        "idsignpost.json",
        "idsignpost.csv",
        "idsignpost.parquet",
        "idsignpost.db",
    ):
        assert f'href="{download}"' in html
    match = re.search(r'id="duckdb-link"\s+href="([^"]+)"', html)
    assert match is not None
    href = match.group(1)
    assert href.startswith("https://shell.duckdb.org/#queries=v0,")
    queries = href.split("#queries=v0,", 1)[1].split(",")
    sql = "\n".join(_duckdb_extraswaps(unquote(part)) for part in queries)
    assert (
        "https://raw.githubusercontent.com/charlesroper/"
        "FSCLegacy-fscbiodiversity.uk-idsignpost/main/idsignpost.parquet"
    ) in sql


def test_pages_workflow_ships_every_web_file_and_the_catalogue_exports() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    for pattern in (
        "src/idsignpost/web/*.html",
        "src/idsignpost/web/*.css",
        "src/idsignpost/web/*.js",
    ):
        assert pattern in workflow
    assert "node_modules" not in workflow
    for name in (
        "idsignpost.json",
        "idsignpost.csv",
        "idsignpost.parquet",
        "idsignpost.db",
    ):
        assert name in workflow
    assert "actions/deploy-pages@v5" in workflow

"""Seam 1: the light and dark palettes in styles.css meet WCAG 2.2 AAA contrast."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

STYLES = (
    Path(__file__).resolve().parents[1] / "src" / "idsignpost" / "web" / "styles.css"
)
PILLS = ("live", "alternative", "broken", "unverified", "no-link")
SURFACES = ("bg", "surface", "surface-alt")
TOKEN_RE = re.compile(
    r"--([a-z0-9-]+):\s*light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)"
)

# Text pairs need 7.0:1 (AAA). Non-text pairs need 3.0:1 (WCAG 1.4.11).
TEXT_PAIRS = (
    [
        (fg, bg)
        for fg in ("text", "text-muted", "link", "heading", "chip-text")
        for bg in SURFACES
    ]
    + [
        ("header-text", "header-bg"),
        ("header-muted", "header-bg"),
        ("footer-text", "footer-bg"),
        ("footer-link", "footer-bg"),
        ("button-text", "button-bg"),
        ("button-secondary-text", "button-secondary-bg"),
        ("chip-text", "chip-bg"),
        ("pill-broken-text", "surface"),
    ]
    + [(f"pill-{p}-text", f"pill-{p}-bg") for p in PILLS]
)
NON_TEXT_PAIRS = (
    [("border", bg) for bg in SURFACES]
    + [("accent", "surface"), ("pill-broken-dot", "surface")]
    + [("focus", bg) for bg in SURFACES]
    + [("focus-inverse", "header-bg"), ("focus-inverse", "footer-bg")]
    + [(f"pill-{p}-dot", f"pill-{p}-bg") for p in PILLS]
)


def _tokens() -> dict[str, dict[str, str]]:
    found = TOKEN_RE.findall(STYLES.read_text(encoding="utf-8"))
    return {name: {"light": light, "dark": dark} for name, light, dark in found}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_formula_matches_published_reference_values() -> None:
    assert _contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert _contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_palette_defines_every_token_in_both_themes(theme: str) -> None:
    tokens = _tokens()
    needed = {name for pair in TEXT_PAIRS + NON_TEXT_PAIRS for name in pair}
    missing = sorted(needed - set(tokens))
    assert not missing, f"Missing light-dark() tokens: {missing}"
    assert all(theme in value for value in tokens.values())


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("foreground", "background"), TEXT_PAIRS)
def test_text_pairs_meet_aaa_contrast(
    theme: str, foreground: str, background: str
) -> None:
    tokens = _tokens()
    ratio = _contrast(tokens[foreground][theme], tokens[background][theme])
    assert ratio >= 7.0, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("foreground", "background"), NON_TEXT_PAIRS)
def test_non_text_pairs_meet_graphical_contrast(
    theme: str, foreground: str, background: str
) -> None:
    tokens = _tokens()
    ratio = _contrast(tokens[foreground][theme], tokens[background][theme])
    assert ratio >= 3.0, f"{theme}: {foreground} on {background} is {ratio:.2f}:1"

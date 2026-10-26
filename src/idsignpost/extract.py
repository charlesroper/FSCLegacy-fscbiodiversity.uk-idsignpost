"""Direct HTML to CatalogEntry extraction for the legacy idsignpost pages."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify

from idsignpost.models import CatalogEntry, ensure_unique_ids
from idsignpost.urls import sanitize_url

FIELD_PREFIX = "field-name-"

VERIFICATION_FIELDS = (
    "web_link_status",
    "web_link_state",
    "web_link_resolved",
    "wayback_link",
    "alternative_link",
    "alternative_probability",
    "alternative_reasoning",
    "verified_at",
)

_SUBMITTED = re.compile(
    r"^Submitted by\s+(?P<name>.+?)\s+on\s+"
    r"(?:[A-Za-z]+,\s*)?(?P<date>\d{2}/\d{2}/\d{4})\s*-\s*(?P<time>\d{2}:\d{2})$",
    re.IGNORECASE,
)

_SPLIT_URL = re.compile(r'<a\s+href="([^"]*)">([^<]*)</a>(@[^\s<]*)')

_MD_TARGET = re.compile(r"\[([^\]]*)\]\((?!<)((?:[^()\s]|\([^()]*\))+)\)")


def clean_text(value: str) -> str:
    """Decode entities twice and collapse legacy whitespace quirks."""
    for _ in range(2):
        value = html.unescape(value)
    value = value.replace("\u00a0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"(?<=\S)[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def slugify(filename: str) -> str:
    """Derive a stable ASCII slug from a legacy HTML filename."""
    stem = filename.rsplit("/", 1)[-1]
    if stem.lower().endswith(".html"):
        stem = stem[: -len(".html")]
    ascii_stem = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive id from filename: {filename!r}")
    return slug


def _field_name(field: Tag) -> str | None:
    for class_name in field.get("class") or []:
        if class_name.startswith(FIELD_PREFIX):
            return class_name[len(FIELD_PREFIX) :].strip("-")
    return None


def _field_items(field: Tag) -> list[Tag]:
    return list(field.select(".field-item"))


def _merge_trailing_link_text(field: Tag) -> None:
    """Fold text just after an anchor into the anchor, repairing split links."""
    for anchor in field.find_all("a"):
        following = anchor.next_sibling
        if isinstance(following, NavigableString) and following.strip():
            continuation = str(following).strip()
            anchor["href"] = str(anchor.get("href", "")) + continuation
            anchor.string = (anchor.get_text() or "") + continuation
            following.extract()


def _fix_split_urls(fragment: str) -> str:
    def replace(match: re.Match[str]) -> str:
        href, text, continuation = match.groups()
        return f'<a href="{href}{continuation}">{text}{continuation}</a>'

    return _SPLIT_URL.sub(replace, fragment)


def _wrap_link_targets(text: str) -> str:
    return _MD_TARGET.sub(lambda m: f"[{m.group(1)}](<{m.group(2)}>)", text)


def to_markdown(fragment: Tag) -> str:
    """Render an HTML fragment as clean Markdown with angle-bracketed links."""
    soup = BeautifulSoup(_fix_split_urls(fragment.decode_contents()), "html.parser")
    for anchor in soup.find_all("a", href=True):
        anchor["href"] = _sanitize_href(str(anchor["href"]))
    rendered = markdownify(str(soup), heading_style="ATX", bullets="-")
    return _wrap_link_targets(clean_text(rendered))


def _first_item_text(field: Tag) -> str | None:
    for item in _field_items(field):
        text = clean_text(item.get_text(" ", strip=True))
        if text:
            return text
    return None


def _sanitize_href(href: str) -> str:
    """Sanitize a link target after the extra entity decode legacy pages need."""
    return sanitize_url(html.unescape(href))


def _first_href(field: Tag) -> str | None:
    anchor = field.find("a", href=True)
    if anchor is not None:
        return _sanitize_href(str(anchor["href"]))
    text = _first_item_text(field)
    if text and text.lower().startswith(("http://", "https://", "www.", "/")):
        return _sanitize_href(text)
    return None


def _hrefs(field: Tag) -> list[str]:
    links = [
        _sanitize_href(str(anchor["href"])) for anchor in field.find_all("a", href=True)
    ]
    if not links:
        text = _first_item_text(field)
        if text and text.lower().startswith(("http://", "https://", "www.", "/")):
            links.append(_sanitize_href(text))
    return list(dict.fromkeys(links))


def _parse_submitted(node: Tag | None) -> tuple[str | None, str | None]:
    if node is None:
        return None, None
    raw = clean_text(node.get_text(" ", strip=True))
    match = _SUBMITTED.match(raw)
    if match is None:
        return raw or None, None
    try:
        parsed = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%d/%m/%Y %H:%M"
        )
    except ValueError:
        return match.group("name"), None
    return match.group("name"), parsed.strftime("%Y-%m-%dT%H:%M:%S")


def parse_entry(html_text: str, filename: str) -> CatalogEntry:
    """Parse one legacy HTML page into a validated CatalogEntry."""
    soup = BeautifulSoup(html_text, "html.parser")
    main = soup.find("div", id="main")
    if main is None:
        raise ValueError(f'{filename}: no <div id="main"> found')

    for node in main.select(".link-wrapper, .tabs"):
        node.decompose()

    title_node = main.find("h1")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    if title_node is not None:
        title_node.decompose()

    submitted_by, submitted_at = _parse_submitted(main.select_one(".meta.submitted"))

    entry_id = slugify(filename)
    data: dict[str, object] = {
        "id": entry_id,
        "filename": filename,
        "title": title or entry_id,
        "submitted_by": submitted_by,
        "submitted_at": submitted_at,
    }

    description: str | None = None
    body_markdown: str | None = None
    available_as: list[str] = []
    additional_links: list[str] = []

    for field in main.select(".field"):
        name = _field_name(field)
        if name is None:
            continue

        if name == "field-web-link":
            _merge_trailing_link_text(field)
            data["web_link"] = _first_href(field)
        elif name == "field-additional-web-link":
            additional_links.extend(_hrefs(field))
        elif name == "field-description":
            items = _field_items(field) or [field]
            markdown = "\n\n".join(part for part in map(to_markdown, items) if part)
            description = markdown or None
        elif name == "body":
            items = _field_items(field) or [field]
            markdown = "\n\n".join(part for part in map(to_markdown, items) if part)
            body_markdown = markdown or None
        elif name == "field-author-s":
            data["authors"] = _first_item_text(field)
        elif name == "field-free":
            value = (_first_item_text(field) or "").lower()
            data["is_free"] = (
                True if value == "yes" else False if value == "no" else None
            )
        elif name == "field-available-as":
            for item in _field_items(field):
                text = clean_text(item.get_text(" ", strip=True))
                if text and text not in available_as:
                    available_as.append(text)
        elif name == "field-taxa":
            data["keywords"] = _first_item_text(field)
        elif name == "comment-body":
            items = _field_items(field) or [field]
            markdown = "\n\n".join(part for part in map(to_markdown, items) if part)
            data["comment_body"] = markdown or None

    data["description"] = description or body_markdown
    data["available_as"] = available_as
    data["additional_web_links"] = additional_links

    if "comment_body" not in data:
        comment = main.select_one(".field-name-comment-body .field-item")
        if comment is not None:
            data["comment_body"] = to_markdown(comment) or None

    return CatalogEntry.model_validate(data)


def merge_verification(
    entries: Iterable[CatalogEntry], previous: Sequence[CatalogEntry]
) -> list[CatalogEntry]:
    """Carry verification results forward for entries whose web_link is unchanged."""
    by_id = {entry.id: entry for entry in previous}
    merged = list(entries)
    for entry in merged:
        old = by_id.get(entry.id)
        if old is None or old.web_link != entry.web_link:
            continue
        for field in VERIFICATION_FIELDS:
            setattr(entry, field, getattr(old, field))
    return merged


def html_files(html_dir: str | Path, limit: int | None = None) -> list[Path]:
    """Return sorted input HTML files, excluding about.html."""
    files = [
        path
        for path in sorted(Path(html_dir).glob("*.html"))
        if path.name.lower() != "about.html"
    ]
    return files[:limit] if limit is not None else files


def extract_catalog(
    html_dir: str | Path, *, limit: int | None = None
) -> list[CatalogEntry]:
    """Parse every legacy HTML page in a directory, rejecting duplicate ids."""
    entries = [
        parse_entry(path.read_text(encoding="utf-8", errors="replace"), path.name)
        for path in html_files(html_dir, limit)
    ]
    ensure_unique_ids(entries)
    return entries


def catalog_json(entries: Iterable[CatalogEntry]) -> str:
    """Serialize entries as deterministic, human-readable JSON."""
    payload = [entry.model_dump() for entry in entries]
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_catalog(entries: Iterable[CatalogEntry], path: str | Path) -> None:
    """Write entries to a catalog JSON file."""
    Path(path).write_text(catalog_json(entries), encoding="utf-8")

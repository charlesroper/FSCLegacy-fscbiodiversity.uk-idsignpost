"""Deterministic publisher pre-resolution before Grounded Search."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import httpx

from idsignpost.gemini_finder import Candidate
from idsignpost.http_checker import BROWSER_HEADERS, DEFAULT_TIMEOUT
from idsignpost.models import CatalogEntry
from idsignpost.urls import host_matches

_SUMMERFIELD_LEGACY_PATH = re.compile(r"([^/]+)~(\d+)")
_ROYENSOC_MEDIA_URL = "https://www.royensoc.co.uk/wp-json/wp/v2/media"
_ROYENSOC_PRODUCT_URL = "https://www.royensoc.co.uk/wp-json/wp/v2/product"
_MIKES_HOME_URL = "https://sites.google.com/view/mikes-insect-keys/home"
_MIKES_ROOT_URL = "https://sites.google.com/view/mikes-insect-keys/mikes-insect-keys"
_MIKES_SITE_PREFIXES = ("/site/mikesinsectkeys", "/site/mikesinsectkeys2")
_MIKES_ROOT_PATHS = frozenset(
    {
        "/view/mikes-insect-keys",
        "/view/mikes-insect-keys/home",
        "/view/mikes-insect-keys/mikes-insect-keys",
    }
)
_MIKES_MAX_SUBINDEXES = 10
_MAX_CANDIDATES = 3
_ROYENSOC_STOPWORDS = frozenset(
    {
        "res",
        "handbook",
        "handbooks",
        "vol",
        "volume",
        "part",
        "parts",
        "section",
        "british",
        "insects",
        "identification",
        "key",
        "keys",
        "for",
        "the",
        "of",
        "and",
        "to",
        "in",
        "a",
        "an",
        "on",
        "with",
        "edition",
    }
)
_MIKES_STOPWORDS = frozenset(
    {
        "key",
        "keys",
        "for",
        "the",
        "of",
        "to",
        "a",
        "an",
        "in",
        "and",
        "identification",
        "british",
        "species",
        "family",
        "beetles",
        "flies",
        "insects",
        "home",
        "mikes",
        "insect",
    }
)


class PreResolver(Protocol):
    """Protocol for deterministic publisher pre-resolution adapters."""

    async def resolve(self, entry: CatalogEntry) -> list[Candidate]: ...


def _clean_summerfield_slug(raw_slug: str) -> str:
    """Normalize a legacy Summerfield Books slug into a WordPress product slug."""
    decoded = unquote(raw_slug)
    ascii_text = (
        unicodedata.normalize("NFKD", decoded)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    without_apostrophes = ascii_text.replace("'", "").replace("’", "")
    slug = re.sub(r"[^a-z0-9-]+", "-", without_apostrophes)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-.")


def _resolve_summerfield(url: str) -> list[Candidate]:
    """Rewrite a legacy summerfieldbooks.com /<slug>~<id> URL when applicable."""
    path = urlsplit(url).path.strip("/")
    if "/" in path:
        return []
    match = _SUMMERFIELD_LEGACY_PATH.fullmatch(path)
    if match is None:
        return []
    clean_slug = _clean_summerfield_slug(match.group(1))
    if not clean_slug:
        return []
    return [
        Candidate(
            url=f"https://www.summerfieldbooks.com/product/{clean_slug}/",
            probability=0.85,
            reasoning="pre-resolved legacy Summerfield Books product URL",
        )
    ]


def _normalize_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _royensoc_pdf_stem(url: str) -> str | None:
    path = unquote(urlsplit(url).path).rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".pdf"):
        return None
    stem = re.sub(r"[^A-Za-z0-9_]+", "", filename[:-4])
    return stem or None


def _distinctive_tokens(
    text: str,
    *,
    stopwords: frozenset[str],
    min_length: int = 2,
    skip_digits: bool = False,
) -> list[str]:
    """Return deduplicated non-stopword alphanumeric tokens in appearance order."""
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", text):
        lowered = token.lower()
        if len(lowered) < min_length or lowered in stopwords:
            continue
        if skip_digits and lowered.isdigit():
            continue
        if lowered not in seen:
            seen.add(lowered)
            tokens.append(token)
    return tokens


def _royensoc_distinctive_tokens(title: str) -> list[str]:
    return _distinctive_tokens(title, stopwords=_ROYENSOC_STOPWORDS, skip_digits=True)


def _rendered_title(item: dict[str, Any]) -> str:
    title_obj = item.get("title")
    if isinstance(title_obj, dict):
        rendered = title_obj.get("rendered")
        if isinstance(rendered, str):
            return rendered
    return ""


def _score_royensoc_media(item: dict[str, Any], clean_stem: str) -> Candidate | None:
    source_url = item.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith(
        ("http://", "https://")
    ):
        return None
    mime_type = str(item.get("mime_type") or "").lower()
    if mime_type and "pdf" not in mime_type and not source_url.lower().endswith(".pdf"):
        return None

    target = _normalize_alnum(clean_stem)
    if not target:
        return None
    file_stem = unquote(urlsplit(source_url).path).rstrip("/").rsplit("/", 1)[-1]
    if file_stem.lower().endswith(".pdf"):
        file_stem = file_stem[:-4]

    best = 0.0
    for raw in (str(item.get("slug") or ""), _rendered_title(item), file_stem):
        norm = _normalize_alnum(raw)
        if not norm:
            continue
        if norm == target:
            best = max(best, 0.95)
        elif norm.startswith(target):
            best = max(best, 0.90)
        elif target in norm:
            best = max(best, 0.80)

    if best <= 0.0:
        return None
    return Candidate(
        url=source_url,
        probability=best,
        reasoning="pre-resolved Royal Entomological Society PDF upload",
    )


def _score_royensoc_product(
    item: dict[str, Any], query_tokens: set[str]
) -> Candidate | None:
    link = item.get("link")
    if not isinstance(link, str) or not link.startswith(("http://", "https://")):
        return None
    if not query_tokens:
        return None
    haystack = " ".join(
        (
            str(item.get("slug") or ""),
            _rendered_title(item),
            urlsplit(link).path,
        )
    )
    item_tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", haystack)}
    overlap = query_tokens & item_tokens
    if not overlap:
        return None
    score = 0.60 + 0.25 * min(len(overlap) / len(query_tokens), 1.0)
    if (
        "/shop/publications/out-of-print-handbooks/" in link
        or "/shop/publications/handbooks/" in link
    ):
        score += 0.08
    return Candidate(
        url=link,
        probability=round(min(score, 0.92), 2),
        reasoning="pre-resolved Royal Entomological Society handbook page",
    )


def _is_mikes_legacy_url(url: str) -> bool:
    if not host_matches(url, ("sites.google.com",)):
        return False
    path = urlsplit(url).path.rstrip("/").lower()
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in _MIKES_SITE_PREFIXES
    )


def _merge_site_links(target: dict[str, str], parsed: dict[str, str]) -> None:
    """Merge extracted site links into target, preferring non-empty labels."""
    for url, label in parsed.items():
        if url not in target or (label and not target[url]):
            target[url] = label


def _extract_mikes_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, str] = {}
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"]).strip()
        if not href:
            continue
        full = urljoin("https://sites.google.com", href)
        parts = urlsplit(full)
        if (parts.hostname or "").lower() != "sites.google.com":
            continue
        clean_path = parts.path.rstrip("/")
        if not clean_path.startswith("/view/mikes-insect-keys"):
            continue
        normalized = urlunsplit(("https", "sites.google.com", clean_path, "", ""))
        label = " ".join(tag.get_text(" ", strip=True).split())
        _merge_site_links(links, {normalized: label})
    return links


def _distinctive_words(text: str, stopwords: frozenset[str]) -> set[str]:
    return {t.lower() for t in _distinctive_tokens(text, stopwords=stopwords)}


def _score_mikes_link(
    cand_url: str,
    anchor_text: str,
    old_slug: str,
    title: str,
) -> Candidate | None:
    cand_slug = urlsplit(cand_url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    old_distinctive = _distinctive_words(old_slug, _MIKES_STOPWORDS)
    title_distinctive = _distinctive_words(title, _MIKES_STOPWORDS)
    source_distinctive = old_distinctive | title_distinctive
    if not source_distinctive:
        return None

    cand_leaf_tokens = _distinctive_words(
        f"{cand_slug} {anchor_text}", _MIKES_STOPWORDS
    )
    overlap = source_distinctive & cand_leaf_tokens
    if not overlap:
        return None
    if old_distinctive and not (old_distinctive & cand_leaf_tokens):
        return None

    ratio = len(overlap) / len(source_distinctive)
    score = 0.55 + 0.30 * ratio
    if old_distinctive and old_distinctive <= cand_leaf_tokens:
        score += 0.10
    if (old_slug and cand_slug == old_slug) or (
        anchor_text and _normalize_alnum(anchor_text) == _normalize_alnum(title)
    ):
        score = max(score, 0.95)
    return Candidate(
        url=cand_url,
        probability=round(min(score, 0.95), 2),
        reasoning="pre-resolved Mike's Insect Keys page",
    )


class PublisherPreResolver:
    """Resolve known migrated publisher URLs via rules and cached site indexes."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._royensoc_lock = asyncio.Lock()
        self._royensoc_media_cache: dict[str, list[dict[str, Any]]] = {}
        self._royensoc_product_cache: dict[str, list[dict[str, Any]]] = {}
        self._mikes_lock = asyncio.Lock()
        self._mikes_index: list[tuple[str, str]] | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the shared async HTTP client, creating one lazily if needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers=BROWSER_HEADERS,
                follow_redirects=True,
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client when owned by this resolver."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _fetch_wp_search(
        self,
        endpoint: str,
        search: str,
        cache: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        key = search.lower()
        async with self._royensoc_lock:
            cached = cache.get(key)
            if cached is not None:
                return cached
            items: list[dict[str, Any]] = []
            try:
                response = await self.client.get(
                    endpoint,
                    params={"search": search, "per_page": 10},
                )
                if 200 <= response.status_code < 300:
                    payload = response.json()
                    if isinstance(payload, list):
                        items = [item for item in payload if isinstance(item, dict)]
            except (httpx.HTTPError, ValueError):
                items = []
            cache[key] = items
            return items

    async def _resolve_royensoc(self, entry: CatalogEntry, url: str) -> list[Candidate]:
        stem = _royensoc_pdf_stem(url)
        ranked: list[Candidate] = []
        seen_urls: set[str] = set()

        if stem is not None:
            media_items = await self._fetch_wp_search(
                _ROYENSOC_MEDIA_URL, stem, self._royensoc_media_cache
            )
            for item in media_items:
                candidate = _score_royensoc_media(item, stem)
                if candidate is not None and candidate.url not in seen_urls:
                    seen_urls.add(candidate.url)
                    ranked.append(candidate)

        if not ranked:
            tokens = _royensoc_distinctive_tokens(entry.title)
            search_terms = " ".join(tokens[:4]) if tokens else (stem or "")
            if search_terms:
                product_items = await self._fetch_wp_search(
                    _ROYENSOC_PRODUCT_URL, search_terms, self._royensoc_product_cache
                )
                query_tokens = {token.lower() for token in tokens}
                if not query_tokens and stem:
                    query_tokens = {stem.lower()}
                for item in product_items:
                    candidate = _score_royensoc_product(item, query_tokens)
                    if candidate is not None and candidate.url not in seen_urls:
                        seen_urls.add(candidate.url)
                        ranked.append(candidate)

        ranked.sort(key=lambda c: c.probability, reverse=True)
        return ranked[:_MAX_CANDIDATES]

    async def _fetch_mikes_links(self, url: str) -> dict[str, str]:
        try:
            response = await self.client.get(url)
        except httpx.HTTPError:
            return {}
        if not response.text:
            return {}
        return _extract_mikes_links(response.text)

    async def _load_mikes_index(self) -> list[tuple[str, str]]:
        async with self._mikes_lock:
            if self._mikes_index is not None:
                return self._mikes_index

            links = await self._fetch_mikes_links(_MIKES_HOME_URL)
            non_root = {
                url: label
                for url, label in links.items()
                if urlsplit(url).path not in _MIKES_ROOT_PATHS
            }
            if not non_root:
                root_links = await self._fetch_mikes_links(_MIKES_ROOT_URL)
                _merge_site_links(links, root_links)

            has_leaves = any(
                len(urlsplit(url).path.strip("/").split("/")) >= 5
                for url in links
                if urlsplit(url).path not in _MIKES_ROOT_PATHS
            )
            if not has_leaves:
                subindexes = [
                    url
                    for url in links
                    if urlsplit(url).path not in _MIKES_ROOT_PATHS
                    and len(urlsplit(url).path.strip("/").split("/")) == 4
                ][:_MIKES_MAX_SUBINDEXES]
                for sub_url in subindexes:
                    sub_links = await self._fetch_mikes_links(sub_url)
                    _merge_site_links(links, sub_links)

            self._mikes_index = [
                (url, label)
                for url, label in links.items()
                if urlsplit(url).path not in _MIKES_ROOT_PATHS
            ]
            return self._mikes_index

    async def _resolve_mikes(self, entry: CatalogEntry, url: str) -> list[Candidate]:
        index = await self._load_mikes_index()
        if not index:
            return []
        old_slug = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1].lower()
        ranked: list[Candidate] = []
        for cand_url, label in index:
            candidate = _score_mikes_link(cand_url, label, old_slug, entry.title)
            if candidate is not None:
                ranked.append(candidate)
        ranked.sort(key=lambda c: c.probability, reverse=True)
        return ranked[:_MAX_CANDIDATES]

    async def resolve(self, entry: CatalogEntry) -> list[Candidate]:
        """Return up to three Candidate URLs for known migrated publishers."""
        url = entry.web_link
        if not url:
            return []
        try:
            if host_matches(url, ("summerfieldbooks.com",)):
                return _resolve_summerfield(url)
            if host_matches(url, ("royensoc.co.uk",)):
                return await self._resolve_royensoc(entry, url)
            if _is_mikes_legacy_url(url):
                return await self._resolve_mikes(entry, url)
        except (httpx.HTTPError, ValueError, UnicodeError):
            return []
        return []

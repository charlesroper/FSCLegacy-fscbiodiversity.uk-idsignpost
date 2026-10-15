#!/usr/bin/env python3
"""Verify URLs, query Wayback Machine snapshots, and find alternatives for broken links.

Per-entry output fields (added to each entry that has a ``Web link``):

- ``Web link status``: HTTP status code or error name (e.g. ``"200"``, ``"404"``, ``"timeout"``).
- ``Web link state``: ``ok`` (reachable), ``broken`` (definitely dead: 404/410/DNS/refused)
  or ``blocked`` (could not be determined: 401/403/429/5xx/timeouts, usually bot protection).
- ``Wayback link``: closest Internet Archive snapshot, if any.
- ``Alternative link`` / ``Alternative probability``: only for ``broken`` links. The
  probability is a heuristic confidence score in [0, 1], not a calibrated probability.

Only definitive results are cached; transient failures (timeouts, 429s, search or
Wayback errors) are retried on the next run.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import difflib
import json
import logging
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from ddgs import DDGS
import httpx

# Keep third-party libraries quiet; only this script logs at INFO.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("verify_urls")
logger.setLevel(logging.INFO)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

REPUTABLE_DOMAINS = {
    "field-studies-council.org",
    "royensoc.co.uk",
    "bto.org",
    "plantlife.org.uk",
    "summerfieldbooks.com",
    "nhbs.com",
    "nhm.ac.uk",
    "biodiversitylibrary.org",
    "cabi.org",
    "cabi.org.uk",
    "abebooks.co.uk",
    "conchsoc.org",
    "britishspiders.org.uk",
    "bsbi.org",
    "kew.org",
    "rhs.org.uk",
    "wildlifetrusts.org",
    "rspb.org.uk",
    "buglife.org.uk",
    "naturenet.net",
}

TRACKING_PARAMS = {"srsltid", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref"}

STATE_OK = "ok"
STATE_BROKEN = "broken"
STATE_BLOCKED = "blocked"


def clean_text_tokens(text: str) -> set[str]:
    """Extract lowercase alphanumeric tokens from text."""
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def host_of(url: str) -> str:
    """Lowercased hostname without a leading ``www.`` prefix."""
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def strip_tracking(url: str) -> str:
    """Remove tracking query parameters (utm_*, srsltid, fbclid, ...)."""
    parts = urlparse(url)
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunparse(parts._replace(query=urlencode(kept)))


def classify_status(code: int) -> tuple[str, bool]:
    """Classify an HTTP status code as (state, cacheable)."""
    if 200 <= code < 400:
        return STATE_OK, True
    if code in (404, 410):
        return STATE_BROKEN, True
    if code in (401, 403):
        # Almost always bot protection or auth walls: unknown, but stable enough to cache.
        return STATE_BLOCKED, True
    # 429, 5xx and anything else: transient, retry on a later run.
    return STATE_BLOCKED, False


class URLVerifier:
    """Handles verification of URLs, Wayback snapshots, and alternative searches."""

    def __init__(
        self,
        cache_file: Path,
        timeout: float = 10.0,
        concurrency: int = 10,
        wayback_concurrency: int = 3,
        min_probability: float = 0.40,
        refresh: bool = False,
        cache_ttl_days: int = 30,
    ) -> None:
        self.cache_file = cache_file
        self.timeout = timeout
        self.min_probability = min_probability
        self.refresh = refresh
        self.cache_ttl = timedelta(days=cache_ttl_days)

        self.live_sem = asyncio.Semaphore(concurrency)
        self.wayback_sem = asyncio.Semaphore(wayback_concurrency)
        self.search_sem = asyncio.Semaphore(2)
        self.concurrency = concurrency

        self.cache: dict[str, dict] = self._load_cache()
        self._dirty_cache_count = 0
        self._inflight: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> dict[str, dict]:
        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    logger.info("Loaded %d cached URLs from %s", len(data), self.cache_file)
                    return data
            except Exception as e:
                logger.warning("Could not read cache file %s: %s", self.cache_file, e)
        return {}

    def save_cache(self) -> None:
        try:
            self.cache_file.write_text(
                json.dumps(self.cache, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._dirty_cache_count = 0
        except Exception as e:
            logger.error("Failed to save cache to %s: %s", self.cache_file, e)

    def _cache_get(self, url: str) -> dict | None:
        """Return a fresh cache entry in the current schema, else None."""
        if self.refresh:
            return None
        cached = self.cache.get(url)
        if not cached or "state" not in cached:
            return None  # missing or written by an older version of this script
        if cached.get("alternative_url") and "fscbiodiversity.uk" in cached["alternative_url"]:
            return None  # stale self-referencing alternative; re-evaluate
        try:
            checked = datetime.fromisoformat(cached["checked_at"])
        except (KeyError, ValueError):
            return None
        if datetime.now(timezone.utc) - checked > self.cache_ttl:
            return None
        return cached

    # --------------------------------------------------------------- liveness
    async def check_liveness(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str, bool]:
        """Return (state, status_str, cacheable) for a URL."""
        async with self.live_sem:
            last_error = "error"
            for attempt in range(2):
                try:
                    resp = await client.head(url, follow_redirects=True, timeout=self.timeout)
                    code = resp.status_code
                    if code >= 400 and code not in (404, 410):
                        # HEAD is often blocked or unsupported; confirm with GET.
                        async with client.stream(
                            "GET", url, follow_redirects=True, timeout=self.timeout
                        ) as stream_resp:
                            code = stream_resp.status_code
                    state, cacheable = classify_status(code)
                    return state, str(code), cacheable
                except httpx.TimeoutException:
                    last_error = "timeout"
                except httpx.ConnectError:
                    last_error = "connect_error"
                except Exception as e:
                    last_error = type(e).__name__.lower()
                await asyncio.sleep(1.0 + attempt)

            if last_error == "connect_error":
                # DNS failure / refused after retries: treat as dead.
                return STATE_BROKEN, last_error, True
            return STATE_BLOCKED, last_error, False

    # ---------------------------------------------------------------- wayback
    async def check_wayback(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str | None, bool]:
        """Return (snapshot_url, lookup_ok). lookup_ok is False on transient errors."""
        api_url = f"https://archive.org/wayback/available?url={quote(url, safe='')}"
        async with self.wayback_sem:
            for attempt in range(3):
                try:
                    resp = await client.get(api_url, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        closest = data.get("archived_snapshots", {}).get("closest")
                        if closest and closest.get("available") and closest.get("url"):
                            snap = closest["url"]
                            if snap.startswith("http://web.archive.org/"):
                                snap = "https://" + snap[len("http://") :]
                            return snap, True
                        return None, True
                    if resp.status_code not in (429, 500, 502, 503, 504):
                        return None, True
                except Exception as e:
                    logger.debug("Wayback lookup failed for %s: %s", url, e)
                await asyncio.sleep(2.0 * (attempt + 1))
        return None, False

    # ----------------------------------------------------------------- search
    def _sync_search_candidates(self, query: str) -> tuple[list[dict[str, str]], bool]:
        """Return (candidates, search_ok). search_ok is False if the engine errored."""
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5))
                return (
                    [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "snippet": r.get("body", ""),
                        }
                        for r in results
                        if r.get("href")
                    ],
                    True,
                )
            except Exception as e:
                logger.debug("Search failed for %r (attempt %d): %s", query, attempt + 1, e)
                if attempt == 0:
                    import time

                    time.sleep(3.0)
        return [], False

    def calculate_probability(
        self,
        candidate_title: str,
        candidate_url: str,
        candidate_snippet: str,
        title: str,
        author: str,
        original_url: str,
        keywords: str,
    ) -> float:
        """Heuristic confidence score in [0.0, 1.0] for an alternative candidate."""
        # 1. Title similarity (weight 0.50)
        seq_ratio = difflib.SequenceMatcher(None, title.lower(), candidate_title.lower()).ratio()
        tokens_title = clean_text_tokens(title)
        tokens_cand_title = clean_text_tokens(candidate_title)
        union = tokens_title | tokens_cand_title
        jaccard_title = len(tokens_title & tokens_cand_title) / len(union) if union else 0.0
        s_title = max(seq_ratio, jaccard_title)

        # 2. Author match (weight 0.20)
        if author:
            author_tokens = [t for t in clean_text_tokens(author) if len(t) >= 3]
            combined_cand = (candidate_title + " " + candidate_snippet).lower()
            s_author = 1.0 if any(t in combined_cand for t in author_tokens) else 0.2
        else:
            s_author = 0.5

        # 3. Domain relevance (weight 0.15)
        orig_host = host_of(original_url)
        cand_host = host_of(candidate_url)
        s_domain = 0.3
        if orig_host and orig_host == cand_host:
            s_domain = 1.0
        elif any(cand_host == rep or cand_host.endswith("." + rep) for rep in REPUTABLE_DOMAINS):
            s_domain = 0.85

        # 4. Keyword / slug overlap (weight 0.15)
        kw_tokens = clean_text_tokens(keywords)
        path_tokens = clean_text_tokens(urlparse(original_url).path)
        interest_tokens = (kw_tokens | path_tokens) - {"http", "https", "html", "php"}
        if interest_tokens:
            cand_tokens = clean_text_tokens(
                candidate_title + " " + candidate_snippet + " " + candidate_url
            )
            s_keywords = min(1.0, len(interest_tokens & cand_tokens) / len(interest_tokens))
        else:
            s_keywords = 0.5

        score = 0.50 * s_title + 0.20 * s_author + 0.15 * s_domain + 0.15 * s_keywords
        return round(score, 2)

    async def find_alternative(
        self,
        client: httpx.AsyncClient,
        original_url: str,
        title: str,
        author: str,
        keywords: str,
    ) -> tuple[str | None, float | None, bool]:
        """Return (url, score, search_ok) for a broken link."""
        if not title:
            return None, None, True

        clean_title_str = re.sub(r"[^\w\s-]", " ", title)
        clean_title_str = re.sub(r"\s+", " ", clean_title_str).strip()
        author_hint = ""
        if author:
            surnames = [w for w in re.findall(r"[A-Za-z]+", author) if len(w) > 3]
            if surnames:
                author_hint = surnames[0]

        query = f'"{clean_title_str}" {author_hint}'.strip()

        async with self.search_sem:
            candidates, search_ok = await asyncio.to_thread(self._sync_search_candidates, query)
            if search_ok and not candidates and author_hint:
                query_fallback = f"{clean_title_str} {author_hint}".strip()
                candidates, search_ok = await asyncio.to_thread(
                    self._sync_search_candidates, query_fallback
                )
            await asyncio.sleep(1.0)  # be polite to the search engine

        if not candidates:
            return None, None, search_ok

        orig_parsed = urlparse(original_url)
        best: tuple[str, float] | None = None

        for cand in candidates:
            cand_url = strip_tracking(cand.get("url", ""))
            if not cand_url:
                continue

            cand_parsed = urlparse(cand_url)
            cand_host = cand_parsed.netloc.lower()
            if cand_host == "fscbiodiversity.uk" or cand_host.endswith(".fscbiodiversity.uk"):
                continue

            if (
                host_of(cand_url) == host_of(original_url)
                and cand_parsed.path.rstrip("/") == orig_parsed.path.rstrip("/")
            ):
                continue

            state, _, _ = await self.check_liveness(client, cand_url)
            if state != STATE_OK:
                continue

            score = self.calculate_probability(
                cand.get("title", ""),
                cand_url,
                cand.get("snippet", ""),
                title,
                author,
                original_url,
                keywords,
            )
            if score >= self.min_probability and (best is None or score > best[1]):
                best = (cand_url, score)

        if best:
            return best[0], best[1], True
        return None, None, True

    # ----------------------------------------------------------------- entries
    async def _verify_url(self, client: httpx.AsyncClient, url: str, entry: dict) -> dict:
        """Verify one URL (deduplicated across entries). Returns a result dict."""
        cached = self._cache_get(url)
        if cached:
            return cached

        state, status_str, live_cacheable = await self.check_liveness(client, url)
        wayback_url, wayback_ok = await self.check_wayback(client, url)

        alternative_url = None
        alternative_prob = None
        search_ok = True
        if state == STATE_BROKEN:
            alternative_url, alternative_prob, search_ok = await self.find_alternative(
                client,
                url,
                entry.get("Title", ""),
                entry.get("Author(s)", "") if isinstance(entry.get("Author(s)"), str) else "",
                entry.get("Keywords", "") if isinstance(entry.get("Keywords"), str) else "",
            )

        result = {
            "status": status_str,
            "state": state,
            "wayback_url": wayback_url,
            "alternative_url": alternative_url,
            "alternative_probability": alternative_prob,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        if live_cacheable and wayback_ok and search_ok:
            self.cache[url] = result
            self._dirty_cache_count += 1
            if self._dirty_cache_count >= 10:
                self.save_cache()
        else:
            logger.info("Not caching %s (transient failure; will retry next run)", url)
        return result

    async def verify_entry(self, client: httpx.AsyncClient, entry: dict) -> dict:
        web_link = entry.get("Web link")
        if not web_link or not isinstance(web_link, str) or not web_link.strip():
            for field in (
                "Web link status",
                "Web link state",
                "Wayback link",
                "Alternative link",
                "Alternative probability",
            ):
                entry[field] = None
            return entry

        url = web_link.strip()
        task = self._inflight.get(url)
        if task is None:
            task = asyncio.ensure_future(self._verify_url(client, url, entry))
            self._inflight[url] = task
        result = await task

        entry["Web link status"] = result.get("status")
        entry["Web link state"] = result.get("state")
        entry["Wayback link"] = result.get("wayback_url")
        entry["Alternative link"] = result.get("alternative_url")
        entry["Alternative probability"] = result.get("alternative_probability")
        return entry

    async def verify_all(self, entries: list[dict]) -> list[dict]:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=self.timeout,
            limits=httpx.Limits(
                max_connections=self.concurrency * 2, max_keepalive_connections=20
            ),
        ) as client:
            total = len(entries)
            completed = 0

            async def process(entry: dict) -> dict:
                nonlocal completed
                res = await self.verify_entry(client, entry)
                completed += 1
                if completed % 10 == 0 or completed == total:
                    logger.info("Progress: %d / %d entries verified", completed, total)
                return res

            return list(await asyncio.gather(*(process(e) for e in entries)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify URLs, query Wayback snapshots, and discover alternative links."
    )
    parser.add_argument("--input", default="idsignpost.json", help="Input JSON catalog")
    parser.add_argument("--output", default="idsignpost.json", help="Output JSON catalog")
    parser.add_argument("--cache-file", default=".url_cache.json", help="Persistent cache file")
    parser.add_argument("--concurrency", type=int, default=10, help="Live check workers")
    parser.add_argument(
        "--wayback-concurrency", type=int, default=3, help="Concurrent Wayback requests"
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout (seconds)")
    parser.add_argument(
        "--min-prob",
        type=float,
        default=0.40,
        help="Minimum heuristic score for alternative URLs (default: 0.40)",
    )
    parser.add_argument(
        "--cache-ttl-days", type=int, default=30, help="Cache entry lifetime in days"
    )
    parser.add_argument("--test", type=int, metavar="N", help="Process only first N entries")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file '%s' not found.", input_path)
        sys.exit(1)

    try:
        entries = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to parse JSON file '%s': %s", input_path, e)
        sys.exit(1)

    if not isinstance(entries, list):
        logger.error("JSON root must be an array of entries.")
        sys.exit(1)

    total_entries = len(entries)
    if args.test is not None:
        target_entries = entries[: args.test]
        logger.info("Test mode: %d of %d entries", len(target_entries), total_entries)
    else:
        target_entries = entries
        logger.info("Verifying %d entries...", total_entries)

    verifier = URLVerifier(
        cache_file=Path(args.cache_file),
        timeout=args.timeout,
        concurrency=args.concurrency,
        wayback_concurrency=args.wayback_concurrency,
        min_probability=args.min_prob,
        refresh=args.refresh,
        cache_ttl_days=args.cache_ttl_days,
    )

    try:
        enriched = asyncio.run(verifier.verify_all(target_entries))
    finally:
        verifier.save_cache()  # keep progress even on Ctrl-C

    if args.test is not None and args.input == args.output:
        final_entries = enriched + entries[args.test :]
    else:
        final_entries = enriched

    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(final_entries, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    states: dict[str, int] = {}
    for e in enriched:
        s = e.get("Web link state")
        if s:
            states[s] = states.get(s, 0) + 1
    logger.info("Wrote %d entries to %s. States: %s", len(final_entries), output_path, states)


if __name__ == "__main__":
    main()

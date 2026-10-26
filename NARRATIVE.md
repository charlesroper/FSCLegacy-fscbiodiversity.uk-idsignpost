# Preserving the ID Signpost: Engineering Journal

A running account of rebuilding the data pipeline that preserves over 700 British and Irish wildlife identification guides.

---

## 1. The challenge: saving a decaying biodiversity index

For decades, the Field Studies Council (FSC) collected recommendations for field identification keys, books, checklists, and guides covering British and Irish wildlife. When the original website was retired, the live index disappeared.

While the raw HTML pages were rescued from website archives, turning raw HTML into a reliable, modern catalogue revealed an immediate problem: severe link rot. Web pages move, publishers change domain structures, and recording schemes migrate to modern platforms. Over 150 links were completely broken, and more than 230 returned bot-detection blocks or transient timeouts.

To make this catalogue genuinely useful, we needed more than a static scrape. We needed an automated pipeline that can:
1. Verify whether each link is still live.
2. Distinguish legitimate resources from subtle redirects (such as domain parking, generic homepages, or soft-404 pages).
3. Search for modern replacements when a guide has moved.
4. Locate historical snapshots on the Internet Archive's Wayback Machine, creating new snapshots for live resources that lack one.

---

## 2. Diagnosing the legacy scripts

The repository originally relied on a chain of standalone scripts in `scripts/` (`extract_idsignpost.py`, `md_to_json.py`, `verify_urls.py`, `json_to_csv.py`, `json_to_parquet.py`). Auditing those scripts and the generated catalogue exposed six structural flaws:

- **Lossy HTML-to-Markdown-to-JSON round-tripping:** `extract_idsignpost.py` converted HTML into `idsignpost.md`, and `md_to_json.py` used regular expressions to parse that Markdown back into JSON. Because Markdown was generated first, `idsignpost.md` never included link health, Wayback snapshots, or alternative URLs.
- **Weakly typed, inconsistent JSON schema:** Keys mixed naming styles (`filename` vs `Title` vs `Author(s)`), booleans were stored as `"Yes"`/`"No"` strings, and fields like `Available as` switched unpredictably between strings and lists.
- **Duplicate-URL cache collisions:** Twenty-seven URLs are shared across multiple distinct guides in the catalogue. Because `verify_urls.py` cached and deduplicated tasks by `url` alone, entry-specific alternative searches overwrote one another whenever two entries shared a broken URL.
- **Fragile search fallback and poor matches:** Scraping DuckDuckGo via `ddgs` triggered rate limits, and simple token-overlap scoring matched broken links to eBay listings, German used-book sites (`zvab.com`), SEO spam blogs, and self-referential signpost aliases (`tombio.uk/idsignpost/`).
- **Blind redirect handling:** The checker followed HTTP redirects automatically without checking whether the destination page actually contained the resource or had bounced to a site homepage.
- **Passive archival coverage:** Seventy-nine working links had no Wayback Machine snapshot at all because the script only queried existing snapshots and never triggered new captures.

---

## 3. Starting from scratch: a clean-slate Python package

Rather than patching the brittle `scripts/` chain, we decided to ditch `scripts/` altogether and build an installable Python 3.12+ package (`src/idsignpost/`) with a unified CLI (`uv run idsignpost`).

### Canonical Pydantic data store
In the new pipeline, `idsignpost.json` is the single canonical source of truth, backed by a strict `CatalogEntry` Pydantic model with `snake_case` fields and normalized types (`is_free: bool | None`, `available_as: list[str]`, split `submitted_by` and `submitted_at` fields).
1. `uv run idsignpost extract` parses `idsignpost/*.html` directly into `CatalogEntry` models and writes `idsignpost.json`.
2. `uv run idsignpost verify` enriches `idsignpost.json` with HTTP status, redirect checks, Wayback snapshots, and Gemini alternatives.
3. `uv run idsignpost export` renders `idsignpost.csv`, `idsignpost.parquet`, and `idsignpost.md` directly from the verified `idsignpost.json` store.

### Two-tier caching
To prevent collisions when multiple guides share a URL, `.url_cache.json` separates **URL-tier** facts (HTTP status, redirect target, Wayback link, keyed by `url`) from **entry-tier** evaluations (Gemini redirect verdicts and alternative candidates, keyed by `f"{entry.id}::{url}"`).

### Grounded alternative discovery and redirect verification
When a link is confirmed broken (or when Gemini rejects a suspicious redirect to a homepage), the pipeline queries `gemini-3.1-flash-lite` using the `google-genai` SDK with Google Search grounding enabled. The model returns up to three ranked candidate URLs with confidence scores and reasoning. Each candidate is filtered against a domain blocklist and checked for live `200 OK` reachability before selection.

### Stratified sampling and offline test suite
To test representative slices of the catalogue rapidly, `sampler.py` draws deterministic stratified samples across link states (`ok`, `broken`, `blocked`, `missing`) and media formats (`--sample N --seed 42`). Every module is backed by an offline `pytest` + `respx` suite in `tests/`, with optional `@pytest.mark.live` smoke tests for live API verification.

---

## 4. First pipeline stage: direct extraction

The first real stage is `src/idsignpost/extract.py`, which reads `idsignpost/*.html` straight into `CatalogEntry` models. The old chain went HTML to Markdown to JSON, so every field passed through a text format that could lose structure. Direct extraction removes that round trip and keeps the HTML as the only source.

The parser handles the quirks that the legacy pages accumulated:

- **Fields are read inside `div#main` only.** Every page also contains a sidebar with search filters, including a second "Free" field. Reading the whole document would let sidebar values override the page being described.
- **Entities are decoded twice.** The pages were saved with double escapes such as `&amp;#13;`, so a single decode leaves literal `&#13;` in the text. Non-breaking spaces become normal spaces and repeated spaces collapse.
- **Hrefs need their own decode.** Entity decoding covers text nodes, but an `&amp;amp;` in an `href` survives as `&amp;` after the parser. Without a second decode, tracking parameters hide behind a broken parameter name and are never stripped. Applying `html.unescape` before sanitization resolved fifteen of the sixteen URL mismatches against the legacy catalogue.
- **Split Flickr URLs are repaired.** Some anchors end early, leaving `@N07/123` as loose text after the link. The trailing text is folded back into the anchor before the href is read.
- **Descriptions become Markdown with angle-bracketed targets.** Links keep their sanitized absolute URL, so relative legacy paths resolve against the archived site and tracking parameters disappear.
- **Verification survives re-extraction.** Fields from `web_link_status` to `verified_at` are copied forward when the id and `web_link` are unchanged, and cleared when the link itself changes. A rebuild must not discard a month of link checks.

The full corpus and the legacy `idsignpost.json` provide a useful audit. All 736 entries match on title, submitted name and timestamp, author, free flag, availability, and keywords. The 127 description differences are the intended consolidation of multi-item descriptions from lists into single strings. One other difference is intentional: `000webhost.com/migrate?utm_source=...` now loses its tracking parameters as the specification requires.

Sanitization also had to stay stable. The legacy page for the BTO video guides carries `%2C` inside a query string, and re-encoding every query value rewrote it as a comma, which would have invalidated the stored verification for those entries. `sanitize_url` now keeps the original query bytes when no tracking parameter is present and only rebuilds the query when something must be removed.

`uv run idsignpost extract --limit 5` prints a sample to stdout, which keeps the canonical file safe during exploration. A full run writes 736 entries to `idsignpost.json`.

---

## 5. Exporters: one catalog, four shapes

`src/idsignpost/export.py` renders the canonical JSON into CSV, Parquet, and Markdown. Every writer runs from the same validated `list[CatalogEntry]`, so no format can drift from the others.

Three rules keep the output trustworthy:

- **Atomic replacement.** Each writer builds a temporary file in the destination directory and replaces the target with `Path.replace`. A crash mid-write cannot leave a half-written catalog, and a reader never sees a partial file. The temporary file is resized to the caller's umask before the move, because `mkstemp` creates files as owner-only and exports should be readable like any other project file.
- **Stable ordering.** Entries are sorted by `id` in all four formats. The same input always produces the same bytes, which makes diffs meaningful and regeneration cheap to review.
- **Schema truth.** JSON keeps the canonical values, including `blocked` for bot-detection responses. The human-facing CSV, Parquet, and Markdown outputs label that state `unverified`, because a blocked link is unknown rather than dead. Parquet writes native types, `bool` for `is_free`, `list<string>` for the two link lists, and `double` for the alternative score, so downstream analytics do not need to parse strings.

CSV uses Python's `csv` module with `newline=""` and joins list fields with `"; "`. Markdown renders each entry under `## <filename>` with the verification fields that the legacy Markdown export never carried: status, state, resolved URL, Wayback link, alternative link, confidence, and reasoning. Multi-line values stay indented two spaces so they remain inside the bullet.

`pyarrow` is imported inside the Parquet writer rather than at module import. Extraction and CSV runs should not pay a heavy import cost for a format they do not touch.

---

## 6. Two-tier cache: separate facts from verdicts

Verification is the expensive part of the pipeline. A single URL can serve several guides, and the old cache keyed everything by URL, so an alternative-link search for one entry could overwrite the search for another. `src/idsignpost/cache.py` splits the cache into two tiers:

- **URL tier** holds facts that belong to the address itself: HTTP status, link state, resolved URL, whether the redirect looked suspicious, the destination page title and heading, and the Wayback snapshot. One record per sanitized URL.
- **Entry tier** holds judgement calls that depend on the guide, not the address: whether a redirect still matches that guide, the alternative URL, its confidence score, and the reasoning. Keys are `"{entry_id}::{url}"`, so shared URLs keep separate verdicts.

Both tiers work the same way. `get_*` returns a record only when it is younger than `ttl_days`, so a stale verdict is simply a miss and the pipeline refreshes it. `put_*` refuses transient results: a timeout or a `503` must not erase a good `200` from an earlier run. Failures that are stable, such as a confirmed `404` or a rejected redirect, are cached, because otherwise every run would pay to rediscover them.

The file carries `schema_version: 2`. Any other version, malformed JSON, or a record without a usable timestamp is discarded rather than partially trusted. Saves go through the same atomic temp-and-replace writer as the catalog, sorted by key for stable diffs, and drop expired records on the way out. Writes trigger a save every 25 changes, and the pipeline saves again on normal exit or interrupt.

---

## 7. HTTP checks: liveness without downloading the library

`src/idsignpost/http_checker.py` decides whether a link still works. It sends a streaming `GET` with ordinary browser headers, follows at most ten redirects, and reads at most 16 KB of the response, and only when the content type is HTML. A PDF guide or a large image never enters memory, but the check still sees the page title and first heading that the redirect analysis needs.

Status codes fall into three groups, and the group decides whether the result may be cached:

- `2xx` and `3xx` are `ok`, `404` and `410` are `broken`, and `400`, `401`, `403`, `405`, and `451` are `blocked`. These verdicts are stable enough to keep.
- `429`, `500`, `502`, `503`, and `504` are `blocked` but transient, as are timeouts and generic protocol failures. Caching them would turn a bad afternoon at one server into permanent link rot.
- A connection failure is `broken` for this run but not cacheable, because the fault is usually ours or the network's, not the site's.

A `400` or `403` gets one plain retry. Bot filters often answer a streaming request with `403` and then serve the same page to an ordinary `GET`, so the first refusal is not a verdict.

Redirects need interpretation, not just following. A cross-domain move, a deep path collapsing to the site root, a soft-404 phrase in the path or title, a landing on a generic shop or search hub when the entry names a specific guide, or a login and consent page all raise `is_suspicious_redirect`. The pipeline passes those cases to Gemini instead of trusting the `200`.

Two operational guards matter more than they look. Redirect comparisons use the same normalization as the cache, so a trailing slash or a `www.` prefix does not count as a move and does not clear verification results. And the checker counts network-level failures; after ten checks with no successful response at all, the run stops instead of rewriting the catalog with outage data. The pipeline enforces that abort before saving anything.

One subtlety cost a test failure: `httpx.InvalidURL` does not inherit from `httpx.HTTPError`. The check catches both, otherwise a malformed legacy URL would abort a run instead of being recorded as `invalid_url`.

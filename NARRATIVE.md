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

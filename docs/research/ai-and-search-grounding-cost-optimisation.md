# AI and Google Search Grounding Cost Optimisation

This document audits how `idsignpost` spends Gemini tokens and Google Search grounding queries, compares `cost.py` against current Google AI pricing, evaluates lower-cost search alternatives and synthesises concrete optimisation levers for design review. Every claim traces to primary sources in `/root/idsignpost` or official vendor documentation.

---

## 1. Repository Audit: Current Call Patterns and Workload

### 1.1 Where Gemini calls and search queries occur

All Gemini network calls sit behind the `Finder` protocol (`src/idsignpost/pipeline.py:62-88`) and `GeminiFinder` (`src/idsignpost/gemini_finder.py:237-437`), bounded by `asyncio.Semaphore(gemini_concurrency)` (`DEFAULT_GEMINI_CONCURRENCY = 3` in `src/idsignpost/pipeline.py:46,275`). Only three operations exist (`GeminiOperation` in `src/idsignpost/cost.py:16-20`):

| Operation | Trigger condition | Search grounding | Model config | Prompt footprint across corpus | Source |
|---|---|---|---|---|---|
| `redirect_verification` | `state == "ok"` and `is_suspicious_redirect` is `True`, with no cached `redirect_is_match` | Off (`search=False`) | `verification_model` (default `gemini-3.1-flash-lite`) | 1,311 to 2,575 chars (avg 1,774 chars, ~444 tokens) | `src/idsignpost/pipeline.py:463-480`, `src/idsignpost/gemini_finder.py:322-355` |
| `candidate_discovery` | `state == "broken"` (including `redirect_mismatch`), `alternative_checked` is `False`, and stored `alternative_link` fails or is absent | On (`search=True`) | `discovery_model` (default `gemini-3.1-flash-lite`) | 564 to 2,486 chars (avg 1,014 chars, ~253 tokens) | `src/idsignpost/pipeline.py:492-494,576-609,673-711`, `src/idsignpost/gemini_finder.py:389-437` |
| `candidate_confirmation` | Candidate passes `_policy_note` and live HTTP `200-399` check (`_facts`), or cached unconfirmed alternative hits `_reconfirm` | Off (`search=False`) | `verification_model` (default `gemini-3.1-flash-lite`) | 1,316 to 3,222 chars (avg 1,728 chars, ~432 tokens) | `src/idsignpost/pipeline.py:626-671,713-758`, `src/idsignpost/gemini_finder.py:357-387` |

Prompt mechanics and cost multipliers observed in `src/idsignpost/gemini_finder.py` and `src/idsignpost/http_checker.py`:

- **Suspicious redirect triggers (`src/idsignpost/http_checker.py:157-162,214-239`):** When `resolved_url` differs from the input URL (via `urls_equal`), `_is_suspicious` fires on host change (`clean_host` differs), root path collapse, soft-404 strings (`404`, `not found`, `page not found`, `error`, `expired`), generic hub paths (`/shop`, `/shop/publications`, `/product-category`, `/resources`, `/search`, `/category`) when `entry_title` is set, or login/consent words (`login`, `signin`, `register`, `subscribe`, `consent`, `cookie`). Non-redirecting `200 OK` pages also trigger `_has_soft_404` on `<title>` and `<h1>`.
- **Prompt contents:** `verify_redirect` sends `title`, `authors`, `description`, `web_link`, `resolved_url`, `page_title`, `page_h1` and `page_snippet` (up to `SNIPPET_CHARS = 800` characters, `src/idsignpost/http_checker.py:15,98`). `confirm_alternative` sends the same entry fields plus `url`, `page_title`, `page_h1` and `page_snippet`. `discover` sends `title`, `authors`, `description`, `available_as`, `keywords`, `web_link` and an optional `hint` (though `Verifier` never passes `hint`, `src/idsignpost/pipeline.py:695`).
- **Response and search-query accounting (`src/idsignpost/gemini_finder.py:107-137,300-320`):** `_GenAIGenerator.generate` reads `prompt_token_count`, `candidates_token_count` and `cached_content_token_count` from `response.usage_metadata`, and sets `search_queries = len(grounding_metadata.web_search_queries)` from `candidates[0]` when `search=True`. Inspection of the installed `google-genai` 2.33.0 SDK shows `GenerateContentResponseUsageMetadata` also carries `thoughts_token_count` and `tool_use_prompt_token_count`, neither of which is captured in `GenerationResult` (`src/idsignpost/gemini_finder.py:68-75,116-120`).
- **Retry behaviour (`src/idsignpost/gemini_finder.py:19-20,226-234,270-298,417-437`):** `_generate` retries `TimeoutError`, HTTP `429` and `5xx` errors up to `MAX_ATTEMPTS = 4` with `BACKOFF = (1.0, 2.0, 4.0)` under a 30-second per-attempt timeout (`DEFAULT_TIMEOUT = 30.0`), recording usage only on attempts that return a response. Separately, `discover()` runs an outer `for attempt in range(2)` loop that repeats the entire grounded search call if `parse_candidates` returns `None` on malformed JSON (`src/idsignpost/gemini_finder.py:417-437`).
- **Budget guard (`src/idsignpost/gemini_finder.py:395-396,418-422`):** `--max-searches` checks `self.total_searches >= self.max_searches` before calling `discover`. Because one `discover` call can emit multiple `web_search_queries` (or run concurrently under `gemini_concurrency = 3`), the cap is soft rather than exact.
- **Origin of `MODEL_RATES` (`src/idsignpost/cost.py:30-37`):** The rate table was authored during the 29 October 2026 cost-tracking milestone (`plans/archive/2026-10-29-gemini-model-optimisation-and-cost-tracking.md:22`, `NARRATIVE.md:285`). An unrecognised model falls back to `(0.25, 1.50, 0.02, 35.0)` for `1.5`/`2.0`/`2.5` tags or `(0.25, 1.50, 0.02, 14.0)` otherwise (`src/idsignpost/cost.py:48-55`).

### 1.2 Catalog and cache workload (`idsignpost.json` and `.url_cache.json`)

Running a read-only audit across `idsignpost.json` and `.url_cache.json` (`.idsignpost_costs.json` is not present in the working directory) shows where calls concentrate:

- **Catalog state distribution (736 entries):** 714 entries carry a `web_link` (22 are `null`), split into 386 `ok`, 290 `broken` and 38 `blocked` (`403`: 15, `400`: 14, `504`: 3, `489`: 2, `429`: 1, `503`: 1, `520`: 1, `timeout`: 1).
- **Broken link breakdown (290 entries):** 186 return `404`, 88 are `redirect_mismatch`, 14 are `connect_error` and two are `protocol_error`. Exactly 93 broken entries hold a confirmed `alternative_link`; 197 have none (117 `404`, 72 `redirect_mismatch`, seven `connect_error`, one `protocol_error`). Of the 197 without an alternative, 196 already hold an `alternative_note` (115 `rejected:`, 79 `ignored:`, one `dropped:`, one `no candidates returned`), and 179 already hold a `wayback_link`.
- **Shared URL duplication:** Across all 714 linked entries, 658 distinct `web_link` URLs exist (27 URLs are shared across 83 entries). Among the 290 broken entries, 272 distinct `web_link` URLs exist (18 shared URLs span 38 entries; 14 entries share identical titles on duplicate URLs, while four URLs serve distinct entries such as two Plantlife wildflower sheets). `Verifier` deduplicates HTTP checks per URL (`src/idsignpost/pipeline.py:281-284`) but invokes `verify_redirect` and `discover` separately per entry (`src/idsignpost/pipeline.py:373-375`).
- **Domain concentration among broken entries (46 domains total):** Five domains account for 220 of the 290 broken entries (75.9%) and 154 of the 197 unrecovered entries (78.2%):

| Broken domain | Total broken | With alternative | Without alternative | Dominant failure mode |
|---|---|---|---|---|
| `sites.google.com` | 79 | 12 | 67 | 78 `redirect_mismatch` to `accounts.google.com` (77 on `/site/mikesinsectkeys`) |
| `www.royensoc.co.uk` | 70 | 27 | 43 | All 70 return `404`; all 27 alternatives sit on `www.royensoc.co.uk` |
| `www.field-studies-council.org` | 35 | 18 | 17 | 29 `404`, six `redirect_mismatch`; 15 alternatives on booksellers (`pemberleybooks.com`, `summerfieldbooks.com`) |
| `www.plantlife.org.uk` | 20 | 0 | 20 | All 20 return `404` (`/download_file/...`); zero alternatives found |
| `www.summerfieldbooks.com` | 16 | 9 | 7 | All 16 return `404`; nine alternatives on booksellers |

- **Redirect verification anomalies:** 101 `ok` entries have a `web_link_resolved` URL, including 19 where the hostname changed. Seven `ok` entries (`key-identification-british-histeridae`, `laemophloeidae`, `leiodidae`, `malachiidae`, `nanophyidae`, `sphindidae` and `keys-identification-british-cryptophagidae`) currently hold `web_link_state == "ok"` despite resolving to `accounts.google.com`. Conversely, 78 `sites.google.com` entries resolving to `accounts.google.com` are marked `redirect_mismatch`.
- **Why rejected discoveries fail (`idsignpost.json`):** Among the 79 `ignored:` notes on unrecovered broken entries, 29 are `ignored: same as the broken link` and 11 are `ignored: site homepage`. Among the 115 `rejected:` notes, 33 sit on `www.royensoc.co.uk` and 30 on `sites.google.com`. On `mikesinsectkeys` (78 broken entries), 12 recovered pages moved to `https://sites.google.com/view/mikes-insect-keys/...` (11 on `sites.google.com`, one on `docs.google.com`), while 66 failed (`36` `ignored:`, `30` `rejected:`).
- **Local `.url_cache.json` state:** Contains 87 `urls` records (`51` `ok`, `23` `blocked`, `13` `broken`, `5` suspicious redirects) and 15 `entries` records (`15` `alternative_checked`, `7` `alternative_confirmed`, `0` redirect verdicts), with `schema_version = 2` (`src/idsignpost/cache.py:17`).

---

## 2. Primary-Source Gemini Pricing, Grounding Billing, and SDK Features

All Gemini API figures below come from Google AI's official Gemini Developer API documentation (`https://ai.google.dev/gemini-api/docs/pricing`, `https://ai.google.dev/gemini-api/docs/grounding`, `https://ai.google.dev/gemini-api/docs/batch-api`, `https://ai.google.dev/gemini-api/docs/caching`, `https://ai.google.dev/gemini-api/docs/models`) and inspection of `google-genai` 2.33.0.

### 2.1 Token, caching, and Batch API pricing

- **Gemini 3.1 Flash-Lite (`gemini-3.1-flash-lite`):** Free tier exists with lower rate limits and data logging (`ai.google.dev/gemini-api/docs/pricing`). Paid tier text rates are **$0.25** per 1M input tokens, **$1.50** per 1M output tokens (including thinking tokens) and **$0.025** per 1M cached read tokens plus **$1.00** per 1M tokens per hour for explicit cache storage (`ai.google.dev/gemini-api/docs/pricing`, `ai.google.dev/gemini-api/docs/models`). Context window is 1,048,576 input / 65,536 output tokens, and thinking depth is configurable.
- **Gemini 2.5 Flash-Lite (`gemini-2.5-flash-lite`):** Paid tier text rates are **$0.10** input, **$0.40** output (including thinking tokens), and **$0.01** cached read per 1M tokens (`$1.00`/1M tokens/hour storage) (`ai.google.dev/gemini-api/docs/pricing`).
- **Gemini 2.5 Flash (`gemini-2.5-flash`):** Marked **deprecated** in Google AI Studio docs (`ai.google.dev/gemini-api/docs/models`). Standard Paid tier text rates are **$0.30** input, **$2.50** output (including thinking tokens) and **$0.03** cached read per 1M tokens (`ai.google.dev/gemini-api/docs/pricing`).
- **Other Flash models in `cost.py`:** `gemini-3.5-flash` is **deprecated** (`ai.google.dev/gemini-api/docs/models`). `gemini-3.5-flash-lite` is listed at **$2.00** input / **$10.00** output / **$0.10** cached through 30 November 2026 (rising to `$4.00`/`$20.00`/`$0.20` on 1 December 2026), and `gemini-3.8-flash` at **$0.75** input / **$3.75** output / **$0.075** cached through 31 December 2026 (rising to `$1.50`/`$7.50`/`$0.15` on 1 January 2027) (`ai.google.dev/gemini-api/docs/pricing`).
- **Batch API (`ai.google.dev/gemini-api/docs/batch-api`, `ai.google.dev/gemini-api/docs/pricing`):** Provides a **50% discount** on input and output token rates for asynchronous jobs with up to a 24-hour target turnaround (inline requests under 20 MB, or JSONL files up to 2 GB via the File API). Batch request configs support `tools` such as `google_search` (`ai.google.dev/gemini-api/docs/batch-api`), though standard Google Search grounding rates still apply to executed searches (`ai.google.dev/gemini-api/docs/pricing`).
- **Context caching (`ai.google.dev/gemini-api/docs/caching`):** Implicit caching is automatic on Gemini 2.5 and newer models; explicit caching guarantees cache retention with hourly storage fees. Minimum input prompt lengths are **4,096 tokens** on Gemini 3 and 4 models and **2,048 tokens** on Gemini 2.5 models (`ai.google.dev/gemini-api/docs/caching`). Single-entry `idsignpost` prompts average 253 to 444 tokens, so they never qualify for caching unless multiple entries are packed into one request.

### 2.2 Google Search grounding billing semantics and SDK constraints

- **Gemini 3 and newer models (per executed query):** Grounding with Google Search is **not available on the Free tier** (`ai.google.dev/gemini-api/docs/pricing`). On the Paid tier, projects receive **5,000 free search requests per month** shared across Gemini 3 and newer models (subject to a default **1,500 RPD** project cap), then pay **$14.00 per 1,000 executed search queries** (`ai.google.dev/gemini-api/docs/grounding`, `ai.google.dev/gemini-api/docs/pricing`). A single prompt can trigger multiple search queries (`web_search_queries`), and each non-empty query is billed separately.
- **Gemini 2.5 and older models (per prompt):** Receive **1,500 free RPD** per project, then pay **$35.00 per 1,000 grounded prompts** (`ai.google.dev/gemini-api/docs/grounding`). A single prompt triggering three search queries on Gemini 2.5 Flash-Lite costs one grounded prompt ($0.035 beyond the free 1,500 RPD, or $0.00 within quota), whereas on Gemini 3.1 Flash-Lite it counts as three executed search queries (consuming three units of the 5,000/month pool or costing $0.042 paid).
- **SDK `exclude_domains` limitation (`google-genai 2.33.0`):** Inspection of `google.genai.types.GoogleSearch` shows `exclude_domains` and `blocking_confidence` carry explicit docstrings stating `"This field is not supported in Gemini API"` (they work only on Vertex AI). Only `search_types` and `time_range_filter` apply in the Gemini Developer API.

### 2.3 Audit of `src/idsignpost/cost.py` `MODEL_RATES` against official pricing

`MODEL_RATES` (`src/idsignpost/cost.py:30-37`) diverges from current official Gemini Developer API pricing in every row except `gemini-3.1-flash-lite` input, output and search rates:

| Model key in `cost.py` | `cost.py` (in / out / cache / search per 1K) | Official Gemini API Paid Tier (in / out / cache / search) | Divergence |
|---|---|---|---|
| `gemini-3.1-flash-lite` | `$0.25` / `$1.50` / `$0.02` / `$14.0` | `$0.25` / `$1.50` / `$0.025` / `$14.0` (after 5,000/mo free) | Cached read is `$0.025`, not `$0.02`; free monthly pool ignored |
| `gemini-2.5-flash-lite` | `$0.075` / `$0.30` / `$0.01875` / `$35.0` | `$0.10` / `$0.40` / `$0.01` / `$35.0` per prompt (after 1,500 RPD free) | Input 25% low, output 25% low, cache 87.5% high; billed per prompt, not per `web_search_queries` |
| `gemini-2.5-flash` | `$0.15` / `$0.60` / `$0.0375` / `$35.0` | `$0.30` / `$2.50` / `$0.03` / `$35.0` per prompt (deprecated) | Output 76% low (`$0.60` vs `$2.50`), input 50% low; deprecated model |
| `gemini-3.5-flash-lite` | `$0.25` / `$1.50` / `$0.02` / `$14.0` | `$2.00` / `$10.00` / `$0.10` (until 30 Nov 2026) / `$14.0` | Token rates 7x to 8x too low |
| `gemini-3.5-flash` | `$0.50` / `$2.00` / `$0.05` / `$14.0` | Deprecated (`ai.google.dev/gemini-api/docs/models`) | Deprecated model |
| `gemini-3.8-flash` | `$0.50` / `$2.00` / `$0.05` / `$14.0` | `$0.75` / `$3.75` / `$0.075` (until 31 Dec 2026) / `$14.0` | Input, output and cache 33% to 47% too low |

In addition, `SessionCostTracker` (`src/idsignpost/cost.py:67-75`) reports nominal list cost without accounting for the 5,000/month free search pool, and `gemini_finder.py:116-120` omits `thoughts_token_count` and `tool_use_prompt_token_count`.

---

## 3. Lower-Cost Search and Pre-Filtering Alternatives

### 3.1 External search APIs for a `SearchProvider` seam

Separating web search from LLM ranking removes Google Search grounding fees ($14.00 per 1,000 queries after the 5,000/month free pool) and lets `idsignpost` query a dedicated search API for candidate URLs and snippets, then rank or confirm them with token-only Gemini calls (~$0.0002 to $0.0003 per entry on `gemini-3.1-flash-lite`).

| Provider | Free tier / credit | Paid rate (per 1,000 queries) | Rate limit & key constraints | Primary source |
|---|---|---|---|---|
| **Google Search Grounding (Gemini 3.1 Flash-Lite)** | 5,000 requests/month on Paid tier (0 on Free tier) | **$14.00** per executed `web_search_queries` entry | 1,500 RPD default; single prompt may execute 2+ queries; `exclude_domains` unsupported | `https://ai.google.dev/gemini-api/docs/pricing`, `https://ai.google.dev/gemini-api/docs/grounding` |
| **Serper.dev** (Google SERP JSON) | 2,500 queries on sign-up (no card required) | **$1.00** (Starter: $50 for 50k credits) down to **$0.30** (Ultimate) | 50 QPS (Starter); prepaid credits expire after 6 months; returns raw Google organic URLs and snippets | `https://serper.dev/` |
| **Brave Search API** (Independent index) | $5.00/month recurring credit (~1,000 queries/month; no rollover; card required) | **$5.00** (Search / LLM Context plan); Answers plan $4.00/1k + $5.00/1M in/out tokens | 50 QPS on Search plan (2 QPS on Answers); independent web index rather than Google SERP | `https://brave.com/search/api/` |
| **Tavily** (AI-formatted search) | 1,000 credits/month (Researcher tier; no card required) | **$8.00** PAYG ($0.008/credit) down to **$5.00** (Growth); 2 credits for `advanced` | 100 RPM on development keys; returns extracted page snippets | `https://tavily.com/`, `https://docs.tavily.com/` |
| **DuckDuckGo (`ddgs`)** | Unofficial scraper (no official web search API) | $0 (scraping) | Frequent `RatelimitException` / HTTP 202 and 403 blocks on datacenter IPs and parallel runs; previously removed from `idsignpost` for brittleness | `https://pypi.org/project/ddgs/`, `NARRATIVE.md:28`, `SPEC.md:14` |
| **Google Custom Search JSON API** | 100 queries/day (existing customers only) | **$5.00** (10,000/day cap) | **Closed to new customers; permanently discontinued on 1 January 2027** | `https://developers.google.com/custom-search/v1/overview` |

At `idsignpost`'s current corpus scale (290 broken entries, or 197 unrecovered), every major API's free tier (Gemini 3+ 5,000/month Paid pool, Serper 2,500 sign-up credits, Brave 1,000/month credits or Tavily 1,000/month credits) covers multiple full-corpus discovery runs at zero marginal search cost, provided queries per entry are bounded. Beyond free tiers, Serper.dev ($1.00/1,000 queries) is 14x cheaper than Gemini 3 Google Search grounding ($14.00/1,000 queries) and returns raw Google SERP results including `site:` filtering.

### 3.2 Deterministic and domain-level strategies grounded in `idsignpost.json`

Catalog statistics show that most Gemini calls target predictable patterns where deterministic code can skip, deduplicate or narrow searches before any model runs:

1. **Deterministic redirect rules before `verify_redirect`:** Seventy-eight broken entries redirect from `sites.google.com` to `accounts.google.com`, and seven `ok` entries also resolve to `accounts.google.com` (`Section 1.2`). Adding deterministic host-level rules for known sign-in or domain-parked destinations (`accounts.google.com` login walls, `000webhost.com`) avoids Gemini redirect checks and prevents false `ok` verdicts.
2. **Entry deduplication on shared URLs:** In `idsignpost.json`, 18 broken URLs are shared across 38 entries, and 14 of those entries carry identical titles (`Section 1.2`). Grouping or caching discovery outcomes by `(cache_key(web_link), normalized_title)` eliminates redundant searches on duplicate entries while preserving entry isolation when titles differ (such as Plantlife's monthly wildflower sheets).
3. **Domain-level migration maps and publisher site-index lookups:** Four domains account for 204 of 290 broken entries (`sites.google.com/site/mikesinsectkeys`: 77, `www.royensoc.co.uk`: 70, `www.field-studies-council.org`: 35, `www.plantlife.org.uk`: 20). Twelve `mikesinsectkeys` pages were already recovered under `https://sites.google.com/view/mikes-insect-keys/...` (`Section 1.2`). Fetching a publisher's `sitemap.xml` or top-level navigation page once over plain HTTP (`HTTPChecker`), extracting links and fuzzy-matching or LLM-ranking locally could resolve dozens of entries across `mikes-insect-keys`, `royensoc.co.uk` and `field-studies-council.org` with zero search API queries.
4. **Skipping or deferring known-hopeless broken entries:** Seventy-nine unrecovered entries failed with `ignored:` (29 repeated the broken URL, 11 returned a homepage) and 115 failed with `rejected:`. Ten `connect_error` entries sit on hosts whose domains no longer resolve (`botanicalkeys.co.uk`, `bristletail.net`, `bmg.b-i-s.org`, `sars.brc.ac.uk`, `NARRATIVE.md:234-238`). Re-running grounded discovery blindly on entries with `404` or dead domains repeats low-yield searches; keeping negative Entry-tier cache records (`alternative_checked = True`) and adding an explicit policy flag for permanently defunct hosts avoids wasted retries.
5. **Fixing `discover()` double-search on JSON parse failure and constraining prompts:** Removing the outer 2-attempt loop in `discover()` (`src/idsignpost/gemini_finder.py:417-437`) or repairing malformed JSON locally avoids doubling search billing on parse errors. Including `site:<clean_host>` or excluding `BLOCKED_HOSTS` in prompt instructions (since `GoogleSearch.exclude_domains` is unsupported in the Gemini Developer API) cuts `ignored: blocked host` and `ignored: same as the broken link` waste (which caused 36 of 79 `ignored:` notes).

---

## 4. Synthesis of Concrete Optimisation Levers

The table below summarises each lever for `/grill-with-docs` evaluation, ranked roughly from lowest to highest implementation complexity.

| Lever | Primary-source cost impact | Quality / recall risk | Implementation complexity |
|---|---|---|---|
| **1. Fix `cost.py` rates, thinking/tool token tracking and free-tier awareness** | Zero direct API savings; fixes 25% to 800% pricing drift (`cost.py:30-37`) and captures `thoughts_token_count` / `tool_use_prompt_token_count` (`gemini_finder.py:116-120`) | None (observability only) | Low (`cost.py`, `gemini_finder.py`) |
| **2. Deterministic login/parked redirect rejection (`accounts.google.com`, etc.)** | Eliminates `verify_redirect` calls (~444 in tokens each) on obvious auth walls; low dollar impact (~$0.0001/call) | Very low; fixes seven false `ok` entries currently resolving to `accounts.google.com` | Low (`http_checker.py`, `pipeline.py`) |
| **3. Remove outer retry in `discover()` + prompt constraints (`site:` / avoid `BLOCKED_HOSTS`)** | Prevents 2x grounded search charges ($0.014+ per extra query) on malformed JSON; cuts `ignored: same as broken link` / `blocked host` waste (~46% of `ignored` notes) | Low; needs response schema (`response_schema`) or tolerant parser to keep parse reliability high | Low (`gemini_finder.py`) |
| **4. Deduplicate `discover` and `verify_redirect` by `(cache_key, title)`** | Saves ~10 to 14 grounded searches per full broken pass (~5% of broken corpus) | None when title and URL match; must preserve entry isolation when titles differ | Low (`pipeline.py`) |
| **5. Publisher sitemap / index pre-resolver (`mikes-insect-keys`, `royensoc.co.uk`, FSC, Plantlife)** | Potentially resolves up to 50 to 150 broken entries with zero search API cost (local HTTP fetch + fuzzy/token-only Gemini match) | Medium-high positive recall gain (`mikesinsectkeys` and `royensoc` dominate unrecovered entries); risk of brittle site-specific parsing if over-specialised | Medium (new optional pre-resolver or `SearchProvider` before web search) |
| **6. Pluggable `SearchProvider` seam (Serper.dev or Brave Search API + token-only Gemini ranking)** | Cuts search cost from **$14.00/1,000** executed queries (Gemini 3) to **$1.00/1,000** (Serper Starter, plus 2,500 free) or **$5.00/1,000** (Brave, $5/mo free), plus ~$0.0002/entry for token-only LLM ranking | Medium; Gemini Search grounding synthesises multi-query reformulations internally, whereas single-query SERP calls may reduce recall unless query formulation is tuned | Medium-high (new `SearchProvider` protocol, config, tests, query builder) |
| **7. Model routing (`gemini-2.5-flash-lite` vs `gemini-3.1-flash-lite` + thinking budget)** | On token-only calls (`verify_redirect`, `confirm_alternative`), `gemini-2.5-flash-lite` ($0.10/$0.40) is ~60% to 73% cheaper than `gemini-3.1-flash-lite` ($0.25/$1.50), saving fractions of a cent per run. On grounded `discover`, Gemini 2.5 gets **1,500 free RPD** (vs 5,000/month on Gemini 3+) but costs **$35/1,000** paid (2.5x higher than Gemini 3+) | Low-medium; requires A/B evaluation on a fixed stratified `--sample` to verify classification and search quality | Low-medium (`cli.py` flags already exist; `gemini_finder.py` needs `thinking_config` if tuning thinking) |
| **8. Gemini Batch API for `verify_redirect` and `confirm_alternative`** | **50% token discount**; saves <$0.05 across a full 736-entry run (because token spend is already tiny relative to search) | Adds up to 24-hour async turnaround and breaks `Verifier`'s inline per-URL HTTP + confirmation loop | High (multi-phase batch state machine for negligible dollar savings) |
| **9. Multi-entry prompt packing for context caching** | Up to 90% input token discount (`$0.25` -> `$0.025`/1M on 3.1 Flash-Lite) once prompts reach >=4,096 tokens | High complexity and higher structured-output parsing error risk for negligible token savings (~$0.01/run) | High (not justified at current corpus size) |

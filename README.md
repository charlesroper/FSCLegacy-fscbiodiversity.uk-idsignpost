# idsignpost

Build and verify a catalog of biodiversity identification resources from the legacy FSC ID Signpost pages.

The package turns the HTML pages in `idsignpost/` into one validated catalog, checks every link, archives what still works, and finds replacements for guides that have moved. Outputs are JSON, CSV, Parquet, and Markdown rendered from the same canonical records.

## Install

The project targets Python 3.12 and uses `uv` for everything.

```bash
uv sync
```

Verification calls the Gemini API. Set the key in your shell before running `verify` or `run`:

```bash
export GEMINI_API_KEY="..."
```

Extraction, export, and the test suite do not need a key.

## Quick start

```bash
# Parse the legacy HTML into idsignpost.json
uv run idsignpost extract

# Check links, look up snapshots, and search for replacements
uv run idsignpost verify

# Render idsignpost.csv, idsignpost.parquet, and idsignpost.md
uv run idsignpost export

# Do all three in one pass
uv run idsignpost run
```

`extract --limit 5` prints a sample to stdout instead of writing the catalog. Use `--output` to write a file explicitly.

`run` extracts the whole corpus, verifies the selected entries, and exports only if verification finishes without aborting.

## Verify a slice

Verification hits the live web, so a full run is slow. Select a smaller set with one of these options:

```bash
# A reproducible cross-section of 40 entries, balanced across link states
uv run idsignpost verify --sample 40

# The first 25 entries in catalog order
uv run idsignpost verify --limit 25

# Only entries already marked broken
uv run idsignpost verify --only-broken
```

`--only-broken` applies before `--sample` or `--limit`. The two selectors cannot be combined. `verify` always loads and writes the full catalog; only the selected entries change.

Useful additions:

- `--refresh` ignores the cache and rechecks everything.
- `--save-archive` asks the Wayback Machine to capture live pages that have no snapshot. Captures run with two workers and a 60 second timeout.
- `--min-prob 0.6` raises the confidence needed before a replacement link is accepted.
- `--model`, `--concurrency`, `--wayback-concurrency`, and `--gemini-concurrency` tune the clients.

## How verification decides

Each entry with a `web_link` passes through the same stages:

1. An HTTP GET classifies the link as `ok`, `broken`, or `blocked`. `blocked` means unverified: the server refused the request or answered with a temporary error.
2. A suspicious redirect (host change, collapse to the site root, soft-404 page, login wall) goes to Gemini for a second opinion. A confirmed match stays `ok`. A mismatch becomes `broken` with `web_link_status = "redirect_mismatch"`.
3. A broken entry asks Gemini, with Google Search grounding, for up to three ranked replacements. Candidates are blocked against known dead hosts, private addresses, the original URL, and a live check before one is stored.
4. The Wayback Machine is queried for a snapshot. Only a live entry can trigger a new capture.
5. When the final state is not `broken`, any old alternative fields are cleared.

Temporary failures never overwrite a good result. A timeout or 5xx leaves the previous state in place and is retried on the next run, and the cache does not store it.

## Cache

`.url_cache.json` has two tiers. The URL tier holds facts about a page, such as status, redirect target, page title, and snapshot. The entry tier holds per-entry verdicts and alternatives. Two catalog entries can point at the same URL without sharing entry verdicts, and the URL is checked once per run.

Records expire after 30 days by default. Pass `--cache-ttl-days` to change that. A cache written by a different schema version is discarded.

## Output

`extract` and `verify` write `idsignpost.json`, a sorted array of validated records. Export renders it to:

| File | Contents |
|------|----------|
| `idsignpost.csv` | Snake case columns, list fields joined with `; ` |
| `idsignpost.parquet` | Native types for booleans, lists, and scores |
| `idsignpost.md` | Readable entries under `## <filename>` |

The canonical state values are `ok`, `broken`, and `blocked`. CSV, Parquet, and Markdown label `blocked` as unverified so readers do not mistake it for a healthy link.

Verification fields include `web_link_status`, `web_link_state`, `web_link_resolved`, `wayback_link`, `alternative_link`, `alternative_probability`, `alternative_reasoning`, `alternative_note`, and `verified_at`.

## Development

Run the same checks as the repository gate:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run ty check
uv run pytest
```

Tests run offline by default and mock network calls with `respx`. The live smoke test is deselected unless you ask for it:

```bash
uv run pytest -m live
```

`SPEC.md` describes the pipeline, schemas, and cache rules in full. `NARRATIVE.md` records why the implementation took the shape it did.

## Legacy scripts

`scripts/` holds the original extractor and verification scripts. The package replaces them for new work. The HTML corpus in `idsignpost/` remains the input for `extract`.

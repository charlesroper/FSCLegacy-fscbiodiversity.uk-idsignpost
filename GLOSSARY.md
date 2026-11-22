# Biodiversity ID Signpost

Preserves and maintains the Field Studies Council catalog of British and Irish wildlife identification resources by extracting legacy HTML records, verifying link health, recovering archived snapshots and working replacement links, and exporting portable datasets.

## Language

**Catalog Entry**:
A single biodiversity identification resource in the catalog, carrying descriptive metadata, an optional primary web link, and verification state.
_Avoid_: Record, item, resource row

**Candidate**:
An unverified replacement URL (paired with a probability score and reasoning) proposed by discovery or carried over from a prior state, awaiting policy, liveness, and confirmation checks.
_Avoid_: Unconfirmed alternative, raw suggestion

**Alternative**:
A replacement URL for a broken Catalog Entry that has passed policy and liveness checks (and confirmation, unless confirmation failed transiently).
_Avoid_: Replacement link, fallback URL, candidate

**URL Tier**:
The shared cache tier keyed by normalized URL that stores network facts (HTTP status, link state, redirect target, page metadata, and Wayback snapshot) across all Catalog Entries referencing that URL.
_Avoid_: Link cache, HTTP cache

**Entry Tier**:
The per-entry cache tier keyed by Catalog Entry ID and normalized URL that stores model judgments (redirect match verdict, Alternative, and evaluation note).
_Avoid_: Verdict cache, record cache

**Transient Failure**:
A temporary network, server (`429` or `5xx`), or model error that is excluded from the cache and never overwrites a previously verified state on a Catalog Entry.
_Avoid_: Soft error, temporary error

**Grounded Search**:
A Gemini discovery call that runs live Google Search queries to find Candidate URLs, counted against `--max-searches` and tracked per query in session costs.
_Avoid_: Web search tool, grounded query

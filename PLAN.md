# Staged Resolution Plan for Code Review Findings

This document records the five-stage plan used to resolve all findings from the Standards and Spec code review of `origin/main...HEAD`.

## Stage 1: Preserve `alternative_note` on Extract and Verify

- **Problem:** `VERIFICATION_FIELDS` was duplicated in `src/idsignpost/extract.py` and `src/idsignpost/pipeline.py`, and `extract.py` omitted `"alternative_note"`. In addition, a transient failure during `Verifier._discover` reset `note` to `None`, clearing an entry's existing `alternative_note`.
- **Resolution:**
  - Define `VERIFICATION_FIELDS` once in `src/idsignpost/models.py` and import it in `extract.py` and `pipeline.py`.
  - Preserve `entry.alternative_note` when `outcome.transient` is `True` in `Verifier._verdict`.
  - Add regression tests in `tests/test_extract.py` and `tests/test_pipeline.py`.

## Stage 2: Validate Alternative Candidates After Redirects

- **Problem:** `GeminiFinder._try_candidate` validated candidate URLs against blocked hosts, image extensions, site homepages, equality with the broken link, and `is_public_http_url` before calling `checker.check`, but did not re-validate `result.resolved_url` when the candidate redirected.
- **Resolution:**
  - Extract `GeminiFinder._policy_note` and apply it both before the HTTP check and to `result.resolved_url` after redirects.
  - Add unit tests in `tests/test_gemini_finder.py` covering redirects to private IP addresses and blocked hosts.

## Stage 3: Refactor Baseline Code Smells

- **Problem:** Duplicated Markdown extraction in `extract.py`, duplicated `_Attempt` vs `DiscoveryOutcome` in `gemini_finder.py`, an unused `WaybackClient.find` method in `wayback.py`, and direct mutation of cache records inside `pipeline.py`.
- **Resolution:**
  - Extract `_field_markdown` in `src/idsignpost/extract.py`.
  - Replace `_Attempt` with `DiscoveryOutcome` in `src/idsignpost/gemini_finder.py`.
  - Remove unused `WaybackClient.find` from `src/idsignpost/wayback.py` and its tests from `tests/test_wayback.py`.
  - Add `EntryRecord.drop_alternative` and `CatalogCache.set_wayback` in `src/idsignpost/cache.py` and call them from `src/idsignpost/pipeline.py`.

## Stage 4: Remove Legacy `scripts/` Directory

- **Problem:** `SPEC.md` specifies discarding the legacy `scripts/` directory, which remained tracked in Git.
- **Resolution:** Delete `scripts/` (`extract_idsignpost.py`, `json_to_csv.py`, `json_to_parquet.py`, `md_to_json.py`, `url_utils.py`, `verify_urls.py`).

## Stage 5: Align `SPEC.md`, `NARRATIVE.md`, and Record `PLAN.md`

- **Problem:** Minor specification drift around `tests/conftest.py` in the directory tree, `GeminiFinder.discover(..., hint=...)`, provisional state recording on unverified transient checks, and `blocked` vs `unverified` labels in JSON vs human-facing exports.
- **Resolution:**
  - Update `SPEC.md` Sections 2.2, 4.2, and 4.3 to match the tested behavior.
  - Document the review and cleanup milestone in `NARRATIVE.md` Section 14.
  - Record this plan in `PLAN.md`.

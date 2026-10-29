---
title: Gemini Model Optimisation and Session Cost Tracking
date: 2026-10-29
status: completed
commit_range: pending
narrative_section: "17"
---

# Gemini Model Optimisation and Session Cost Tracking

## Goal
Optimise Gemini model usage across pipeline operations, strip unneeded Google Search grounding from redirect verification and candidate confirmation to stop runaway search costs, add a `--max-searches` safety limit, and implement local session-based cost tracking with an on-demand `idsignpost cost` command.

## Agreed Architecture Decisions
1. **Model Assignment & Search Grounding**:
   - Support separate `--verification-model` and `--discovery-model` CLI options, both defaulting to `gemini-3.1-flash-lite` (with `--model` setting both when provided).
   - Strip Google Search grounding completely from `verify_redirect` and `confirm_alternative`. Both evaluate scraped HTML metadata (`title`, `<h1>`, body snippet) against catalog entry fields; they do not need web search.
   - Keep Google Search grounding exclusively for broken link alternative discovery (`discover`).
   - Add a `--max-searches` option to cap Google Search queries per run and prevent unexpected costs.
2. **Cost Calculation**:
   - Extract prompt tokens, completion tokens, cached tokens, and Google Search query counts directly from Gemini response objects (`usage_metadata` and `grounding_metadata.web_search_queries`).
   - Calculate nominal list prices using a built-in pricing table for standard models (Gemini 3.1 Flash Lite, 2.5 Flash Lite, 2.5 Flash, 3.5/3.8 Flash, and Google Search grounding queries).
3. **Real-Time Run Reporting**:
   - At the conclusion of `idsignpost verify` and `idsignpost run`, display an itemised cost block categorised by operation type (redirect checks, discovery searches, candidate confirmations), showing tokens, search queries, run dollar cost, and updated session grand total.
4. **Session Storage & On-Demand Command**:
   - Persist session cost history locally in `.idsignpost_costs.json`, excluded from git via `.gitignore`.
   - Accumulate runs across the session, automatically rolling over if 24 hours of inactivity elapse between runs.
   - Add `idsignpost cost` command to display an aligned terminal table of run line items and the session grand total.
   - Support `idsignpost cost --reset` to clear the running session, and `idsignpost cost --json` for programmatic export.

## Stages Completed
- [x] Stage 1: Build local cost tracking module (`src/idsignpost/cost.py`) and update `.gitignore`
- [x] Stage 2: Optimise GeminiFinder (`src/idsignpost/gemini_finder.py`)
- [x] Stage 3: Wire cost tracker into pipeline and CLI (`src/idsignpost/pipeline.py` & `cli.py`)
- [x] Stage 4: Verification, documentation, and narrative

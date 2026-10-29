# Project Plans

This directory preserves plans, stage breakdowns, and execution blueprints for milestones in this repository.

## Plan Lifecycle

To keep the workspace clean and avoid confusing agents with stale task lists:

1. **Active plans:** While a multi-stage milestone is in progress, its blueprint lives at the repository root as `PLAN.md`.
2. **Execution:** Stages and checklists in `PLAN.md` are updated as work proceeds.
3. **Archival:** When all stages pass quality gates and are committed:
   - High-level design rationale and milestones are summarized into `NARRATIVE.md`.
   - Architectural and schema changes are codified in `SPEC.md`.
   - `PLAN.md` is moved to `plans/archive/YYYY-MM-DD-<slug>.md` with frontmatter recording its date, status, commit range, and related narrative sections.
   - The root `PLAN.md` is removed.
   - The index below is updated.

## Plan Archive

| Plan | Date | Status | Commits | Summary |
|---|---|---|---|---|
| [Code Review Resolution](archive/2026-10-27-code-review-resolution.md) | 2026-10-27 | Completed | `791f554..e9a0323` | Fix candidate redirect validation, deduplicate verification fields, remove scripts, add Wayback fallback |
| [Gemini Model Optimisation and Cost Tracking](archive/2026-10-29-gemini-model-optimisation-and-cost-tracking.md) | 2026-10-29 | Completed | Pending | Scope search grounding to discovery, split models, add session cost tracking and on-demand cost command |

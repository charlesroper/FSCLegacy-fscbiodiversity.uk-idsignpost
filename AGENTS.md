# AGENTS.md

Instructions and standards for agents working on this repository.

## Environment and tooling

- **Python version:** Target Python 3.12+. Use modern Python syntax and standard library features.
- **Package management:** Use `uv` exclusively for managing dependencies and running commands (`uv run <command>`, `uv sync`). Do not invoke raw pip directly.
- **Code formatting and linting:** Run `uv run ruff check --fix .` and `uv run ruff format .` after modifying Python files.
- **Type checking:** Check static types using `uv run ty check`.
- **Testing:** Run tests using `uv run pytest`. Keep tests fast and offline by default, mocking network calls with `respx`.
- **Cross-platform and shell compatibility:** Solutions and commands must work across Windows (PowerShell), WSL2 Ubuntu (Bash), WSL2 Arch (Fish) and Debian on PRoot:
  - Prefer Python (`uv run python`, `pytest`, `pathlib`) over shell scripts or POSIX utilities (`grep`, `sed`, `awk`, `/bin/sh`).
  - Always invoke project tools via `uv run <command>` rather than `.venv/bin/*` or inline `VAR=val cmd` prefixes.
  - Create git worktrees inside `.worktrees/<name>` (gitignored on the same filesystem) and run `uv run <command>` inside each worktree; never place worktrees in `/tmp`.
  - Never share a `.venv` directory between Windows and WSL2; use separate clones or set `UV_PROJECT_ENVIRONMENT` in WSL2.
  - For multi-line `git commit` or `gh` bodies, use multiple `-m` flags, `-F <path>` or `--body-file <path>` instead of shell heredocs (`<<'EOF'`).
- **Virtual environment on filesystems without symlinks:** If `uv sync` fails with `Permission denied` while creating `.venv/bin/python` or `.venv/lib64`, the workspace does not support symlinks. Put the environment outside the repo and use copy mode:
  ```bash
  export TMPDIR=/tmp/opencode
  export UV_PROJECT_ENVIRONMENT=/tmp/opencode/idsignpost-venv
  uv sync --link-mode copy
  ```
  If a cached wheel extracts incompletely, reinstall with `uv sync --reinstall --no-cache --link-mode copy`. Keep `TMPDIR` set for `uv run` commands. Do not commit the external environment path.

## Prose and documentation style

- **No em-dashes:** Do not use em-dashes anywhere in prose (Markdown documents, docstrings, commit messages, code comments). Rewrite sentences with commas, colons, parentheses, periods, or conjunctions.
- **Clear technical writing:** Follow William Zinsser principles (see `.agents/skills/on-writing-well`). Lead with the core message, use short familiar words, choose active verbs, and cut filler adjectives.
- **Running narrative:** Maintain `NARRATIVE.md` as an ongoing blog-style record of architecture choices, milestones, and design rationale. Update this document after completing major features or refactoring work. Include the "why" as well as the "what".
- **Technical specification:** Consult `SPEC.md` for the verification pipeline architecture, data schemas, caching rules, and test requirements before modifying pipeline code.
- **AI model:** Use `gemini-3.1-flash-lite` as the default model for discovery and verification tasks.

## Git workflow and commit conventions

- **Commit policy:** For every commit, read and follow `/root/.config/agent/commit-policy.md`.
- **No em-dashes in commits:** Use commas, colons, parentheses, or periods instead of em-dashes in commit subjects and bodies.
- **Pre-commit quality gate:** Before committing, ensure the working tree passes `uv run ruff check --fix .`, `uv run ruff format .`, `uv run ty check`, and `uv run pytest`.
- **Atomic milestones:** Keep commits focused on a single logical change. Do not squash unrelated stages into monolithic commits.

## Planning and plan lifecycle

- **Active plans:** Use `PLAN.md` at the repository root exclusively for active, multi-stage tasks. Keep checkboxes and stage resolutions updated as work proceeds.
- **Archiving completed plans:** When a plan is complete, do not leave it at the repository root. Archive it to `plans/archive/YYYY-MM-DD-<slug>.md` with metadata (title, date, status, commit range, narrative sections), record an entry in `plans/README.md`, and delete the root `PLAN.md`.
- **Preserving context:** Summarize the architectural reasons and milestone outcomes in `NARRATIVE.md` and update `SPEC.md` if schemas or behaviors changed.

## Agent skills

### Issue tracker

GitHub Issues on `charlesroper/FSCLegacy-fscbiodiversity.uk-idsignpost` via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`GLOSSARY.md` and `docs/adr/` at repo root). See `docs/agents/domain.md`.

## Writing framework

Follow `/root/.config/agent/writing-policy.md` for section-level Stage 1 routing across `on-writing-well` (`Longform Prose`, `Technical Docs & READMEs`, `Web & Content Design`, `UX & Microcopy`), `ste-80` and `project-narrative`, followed by the Stage 2 `guardian-style-guide` sub-editor gate (commit messages follow `/root/.config/agent/commit-policy.md`). See `CODING_STANDARDS.md` for review-time judgement standards.

Two-stage Editorial Pipeline for authored documentation, copy and commit messages:

### Stage 1: Drafting

Route drafting to the active specialist skill based on Surface and Reader Job:
- `on-writing-well`: Human-centred prose, conceptual overviews, developer guides, README introductions, web content design and interface microcopy.
- `ste-80`: Operational software documentation, runbooks, deployment procedures, CLI flag and command specifications, API guides and troubleshooting steps.
- `project-narrative`: Evidence-linked project narrative and historical milestone posts (`NARRATIVE.md`).
- `cbeams-git-commit-messages`: Git commit subject lines and bodies.

### Stage 2: Sub-Editor Gate

Run all drafted prose, documentation and copy through `guardian-style-guide` before delivery (commit messages are exempt):
- Enforce British English spelling (`-ise`, `-our`), down-style capitalisation and Guardian house style.
- Validate typography and punctuation: double quotation marks for speech, straight quotes in Markdown, zero unspaced em-dashes. Spaced en-dashes (` - ` or commas/colons/parentheses per repository rules) are permitted for parenthetical pauses.
- Run trope check against AI writing cliches and brochure adjectives.
- Preserve authorial voice, structural design and verbatim quotations.

# AGENTS.md

Instructions and standards for agents working on this repository.

## Environment and tooling

- **Python version:** Target Python 3.12+. Use modern Python syntax and standard library features.
- **Package management:** Use `uv` exclusively for managing dependencies and running commands (`uv run <command>`, `uv sync`). Do not invoke raw pip directly.
- **Code formatting and linting:** Run `uv run ruff check --fix .` and `uv run ruff format .` after modifying Python files.
- **Type checking:** Check static types using `uv run ty check`.
- **Testing:** Run tests using `uv run pytest`. Keep tests fast and offline by default, mocking network calls with `respx`.
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

- **Commit style:** Follow Chris Beams' seven rules for all commit messages:
  1. Separate subject from body with a blank line.
  2. Limit the subject line to 50 characters (72 is the hard ceiling).
  3. Capitalize the subject line.
  4. Do not end the subject line with a period.
  5. Use the imperative mood in the subject line (e.g. "Add", "Fix", "Refactor", "Implement").
  6. Wrap the body at 72 characters.
  7. Use the body to explain what and why instead of how.
- **No em-dashes in commits:** Use commas, colons, parentheses, or periods instead of em-dashes in commit subjects and bodies.
- **Pre-commit quality gate:** Before committing, ensure the working tree passes `uv run ruff check --fix .`, `uv run ruff format .`, `uv run ty check`, and `uv run pytest`.
- **Atomic milestones:** Keep commits focused on a single logical change. Do not squash unrelated stages into monolithic commits.

## Planning and plan lifecycle

- **Active plans:** Use `PLAN.md` at the repository root exclusively for active, multi-stage tasks. Keep checkboxes and stage resolutions updated as work proceeds.
- **Archiving completed plans:** When a plan is complete, do not leave it at the repository root. Archive it to `plans/archive/YYYY-MM-DD-<slug>.md` with metadata (title, date, status, commit range, narrative sections), record an entry in `plans/README.md`, and delete the root `PLAN.md`.
- **Preserving context:** Summarize the architectural reasons and milestone outcomes in `NARRATIVE.md` and update `SPEC.md` if schemas or behaviors changed.

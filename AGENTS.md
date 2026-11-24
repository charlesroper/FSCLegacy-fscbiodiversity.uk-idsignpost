# AGENTS.md

Repository instructions for coding agents. Check `pyproject.toml` and `.github/workflows/ci.yml` for tool config.

## Environment and checks

- **Toolchain:** Target Python 3.12+ and manage all Python tools exclusively via `uv run` (`uv run ruff check --fix .`, `uv run ruff format .`, `uv run ty check`, `uv run pytest`). Keep tests offline by default using `respx`.
- **Cross-platform / PRoot:** Before creating worktrees, handling heredocs or setting up virtual environments on filesystems without symlinks, read `docs/agents/tooling.md`.
- **Web layout checks:** Before editing `src/idsignpost/web/` layout or theme rules, read `docs/agents/browser-verification.md`.
- **AI model default:** Use `gemini-3.1-flash-lite` for discovery and verification tasks.

## Documentation and plans

- **No em-dashes:** Never use em-dashes (`\u2014`) in Markdown, code, comments, UI assets or commit messages.
- **Editorial pipeline:** Follow `docs/agents/writing-policy.md` for Section-level Stage 1 routing (`on-writing-well`, `ste-80`, `project-narrative`) and Stage 2 `guardian-style-guide` checks. Follow `CODING_STANDARDS.md` for review-time judgement standards.
- **Living docs:** Consult `SPEC.md` before modifying pipeline schemas or CLI behaviour; record architectural milestones and design rationale in `NARRATIVE.md`.
- **Plans lifecycle:** Track active multi-stage plans in root `PLAN.md`. When complete, archive to `plans/archive/YYYY-MM-DD-<slug>.md`, record an entry in `plans/README.md` and remove root `PLAN.md`.

## Git and project conventions

- **Commit policy:** Read and follow `docs/agents/commit-policy.md` for every commit, and pass the pre-commit quality gate (`ruff`, `ty`, `pytest`) beforehand. Keep commits atomic.
- **Issue tracker & labels:** `charlesroper/FSCLegacy-fscbiodiversity.uk-idsignpost` via `gh`. See `docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`.
- **Domain glossary:** Single-context glossary in `GLOSSARY.md` (`docs/adr/` when ADRs exist). See `docs/agents/domain.md`.

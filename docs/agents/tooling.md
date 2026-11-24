# Environment and Shell Tooling

Platform notes for Windows (PowerShell), WSL2 Ubuntu (Bash), WSL2 Arch (Fish) and Debian on PRoot.

## Shell and virtual environment rules

- Prefer Python (`uv run python`, `pytest`, `pathlib`) over shell utilities (`grep`, `sed`, `awk`, `/bin/sh`).
- Always invoke project tools through `uv run <command>` rather than `.venv/bin/*` or inline `VAR=val cmd` prefixes.
- Create git worktrees inside `.worktrees/<name>` (gitignored on the same filesystem) and run `uv run <command>` inside each worktree; do not place worktrees in `/tmp`.
- Keep `.venv` separate between Windows and WSL2 (use separate clones or `UV_PROJECT_ENVIRONMENT` in WSL2).
- For multi-line `git commit` or `gh` bodies, pass multiple `-m` flags, `-F <path>` or `--body-file <path>` instead of shell heredocs.

## Filesystems without symlinks (PRoot)

If `uv sync` fails with `Permission denied` while creating `.venv/bin/python` or `.venv/lib64`, place the environment outside the repository in copy mode:

```bash
export TMPDIR=/tmp/opencode
export UV_PROJECT_ENVIRONMENT=/tmp/opencode/idsignpost-venv
uv sync --link-mode copy
```

If a cached wheel extracts incompletely, reinstall with `uv sync --reinstall --no-cache --link-mode copy`. Keep `TMPDIR` set for `uv run` commands and never commit the external environment path.

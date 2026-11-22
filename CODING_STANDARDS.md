# Coding Standards

Judgement-call standards for `/code-review` and contributors in this repository. Deterministic rules (formatting, lint, static types and em-dash checks) run via `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pytest`.

## Domain vocabulary

- Use canonical terms from `GLOSSARY.md`: **Catalog Entry**, **Candidate**, **Alternative**, **URL Tier**, **Entry Tier**, **Transient Failure** and **Grounded Search**.
- Reject synonyms listed under `_Avoid_` in `GLOSSARY.md` across code, tests, CLI output and documentation.

## Module depth and seams

- Prefer deep modules with small interfaces over thin pass-through wrappers.
- Bundle four or more repeated dependency or adapter parameters into a frozen slotted dataclass (such as `VerifyDeps`) instead of forwarding long parameter lists across call sites.
- Keep seams justified by real adapters (for example, production HTTP or Gemini clients paired with offline test fakes); avoid speculative single-adapter wrappers.
- Test observable behaviour through public module interfaces (`Verifier.verify`, `run_verification`, `CatalogCache` methods, `main`) rather than reading private fields or monkeypatching internal hooks.

## Cross-platform Python

- Target Python 3.12+ and preserve portability across Windows (PowerShell), WSL2 Ubuntu (Bash), WSL2 Arch (Fish) and Debian on PRoot.
- Prefer Python (`pathlib.Path`, `pytest`, `uv run`) over shell scripts or POSIX-only utilities (`grep`, `sed`, `awk`, `/bin/sh`).
- Specify `encoding="utf-8"` explicitly on all text file reads and writes.
- Close open file handles before calling `Path.replace()` so atomic file writes succeed on Windows as well as POSIX systems.

## Editorial and prose standards

- Follow `docs/agents/writing-policy.md` and `AGENTS.md`: route each documentation section by reader job (`on-writing-well`, `ste-80` or `project-narrative`), then pass prose through `guardian-style-guide`.
- Use British English (`-ise`, `-our`), down-style headings and zero Oxford commas unless needed to prevent ambiguity.
- Never include em-dashes (`\u2014`) anywhere in prose, docstrings, comments or commit messages.

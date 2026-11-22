# Writing defaults

Apply to authored deliverables: documentation, READMEs, guides, runbooks,
release notes, pull request descriptions, web copy, UX microcopy and
`NARRATIVE.md`.

Do not apply to conversational replies, code comments or commit messages.

## Route by section, not by document

A document can contain several reader jobs. Route the smallest section that
has a distinct job, not the whole file.

| Section job | Specialist |
| --- | --- |
| Overview, motivation, conceptual explanation, release notes, web copy, UX microcopy | `on-writing-well` |
| Installation, configuration, migration, rollback, runbook, CLI or API reference, troubleshooting, error tables, pull request descriptions | `ste-80` |
| `NARRATIVE.md` and project history | `project-narrative` |
| Commit messages | `docs/agents/commit-policy.md`, not this policy |

A README can use `on-writing-well` for its introduction and `ste-80` for its
Install, Update and Troubleshooting sections. Keep one specialist per
paragraph; switch at a heading or list boundary.

Load each specialist only when its section is in scope. For a mixed document,
load each specialist once.

## Stage 2: sub-editor gate

Run `guardian-style-guide` on each drafted prose section, except commit
messages. Check British English, Guardian house style, typography and AI
tropes, and preserve voice and structure.

Review prose only. Do not rewrite code blocks, command syntax, CLI flags,
API names, configuration keys or error text. `ste-80` mechanics still govern
operational steps, commands and terminology.

Apply Guardian rulings consistently across sections so routing does not
fragment voice or terminology.

## Precedence

A project `AGENTS.md`, `Writing framework` section or `Writing policy:` line
overrides this default. Repository house rules outrank both skills.

## Readiness

A deliverable is ready when every section has one Stage 1 specialist, every
prose section passed the Stage 2 gate, and terminology and voice hold across
section boundaries.

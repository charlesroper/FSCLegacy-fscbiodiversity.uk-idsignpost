# Commit defaults

Apply to every project unless its `AGENTS.md` says otherwise.

Apply both skills to any commit. Load them when their content is not
already in context; do not assume rules from memory if a skill has changed.

1. `cbeams-git-commit-messages`: draft the subject and body. Its wording
   rules win over examples in other skills.
2. `git-commit`: stage files, check for in-progress operations, split
   unrelated changes and run the commit. Its safety rules win.

Message shape: a Conventional header plus a cbeams body.

- Header: `<type>(<scope>): <summary>`, imperative, lowercase summary,
  72 characters maximum, no trailing full stop.
- Body: explain what changed and why, wrapped at 72 characters.
- Repository rules, hooks and a project `Commit policy:` line outrank both
  skills.

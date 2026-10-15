@AGENTS.md

Claude-specific: path-scoped rules are in `.claude/rules/` (the PHP client, the worker, the MCP server),
`/check` runs what CI runs, and the post-edit hook reports a PHP file without `declare(strict_types=1)` or
with tabs, and a worker module that imports a model dependency at top level: fix it before moving on.
Permissions in `.claude/settings.json` allow the exact compose commands CI runs and read-only git and gh,
ask before `gh workflow run`, `gh pr create`, `gh pr merge` and the model `fetcher`, and deny pushes to
`main`, force pushes and reading `.env`. Your own notes go in `CLAUDE.local.md` (ignored).
Multi-step work: the user-level `/autonomous-delivery` skill and its `delivery-reviewer` agent follow
this file's rules.

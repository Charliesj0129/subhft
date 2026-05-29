# Git Workflow

Use conventional commits: `feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `test:`, `chore:`, `ci:`, `alpha:`.

Hygiene:

- Preserve user work; inspect status before staging or committing.
- Stage/commit intentional files only.
- Never commit secrets, `.env*`, `config/settings.py`, data/WAL/report exports, caches, `.benchmarks/`, IDE files.
- No new `type: ignore` or `noqa` without a specific justification.
- Rust changes require clippy/test; Python changes require verification matching blast radius.
- Worktrees/stashes/branches are temporary; clean up agent-created worktrees before session end.
- Never push ephemeral `worktree-agent-*` branches.

Destructive git operations require explicit user request.

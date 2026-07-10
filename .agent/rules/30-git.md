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

Branch discipline (branch-per-theme):

- One branch = one theme. A new theme (feature, migration, research lane,
  governance wave) starts a new branch named for it — never appended to
  whatever branch is checked out. (The rule's origin: the
  docs/agent-knowledge-distillation branch accumulated shioaji + ops +
  research + agent-system commits, making review and rollback hard. Branch
  names in governing docs stay un-backticked — the agent-docs path checker
  reads backticked path-shaped tokens as file claims.)
- `.agent/memory/current_session.md` keeps the branch registry (purpose +
  expected lifetime per branch); update it when creating or retiring one.
- Branch creation is cheap and ask-free; merges go through review gates;
  deletion/rebase stay destructive → explicit user request.

Destructive git operations require explicit user request.

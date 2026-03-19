# Git Hygiene Rules

## Session Cleanup Protocol

After every agent session that creates worktrees:
1. List worktrees: `git worktree list`
2. Remove stale worktrees: `git worktree prune`
3. Delete orphaned worktree directories under `.claude/worktrees/`
4. Verify: `du -sh .claude/worktrees/` should be minimal

## Untracked Code Audit

Before ending a session:
1. Run `git status --short | grep "^??"` to list untracked files
2. Files in `src/` or `tests/` MUST be either:
   - Staged and committed (if intentional)
   - Added to `.gitignore` (if generated/temporary)
   - Deleted (if accidental)
3. Never leave source/test files untracked across sessions

## Worktree Policy

- Worktrees are ephemeral — do NOT accumulate them
- Maximum worktree age: 24 hours
- `.claude/worktrees/` is gitignored; contents are disposable
- After merging a worktree branch, always prune: `git worktree prune`

## Branch Hygiene

- Delete feature branches after merge
- Prune remote tracking branches: `git fetch --prune`
- Keep `main` as the only long-lived branch

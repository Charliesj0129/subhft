# Git Workflow & Hygiene

## Commit Messages

Conventional commit prefixes:
- `feat:` new feature, `fix:` bug fix, `refactor:` no behavior change
- `perf:` performance, `docs:`, `test:`, `chore:`, `ci:`
- `alpha:` alpha strategy research/implementation

Examples:
- `feat(strategy): add Hawkes-process MM strategy`
- `fix(normalizer): handle one-sided LOB snapshots`
- `perf(lob): migrate compute_stats to Rust`

## Branch Strategy

- `main` is the primary branch; all development happens on `main`.
- Feature branches optional: `feature/<short-desc>` or `alpha/<alpha-id>`.
- Use `git stash` or worktrees (`git-parallel` skill) for context switching.

## DO NOT Commit

- `.env`, `.env.prod`, credentials
- `config/settings.py` (per-machine overrides)
- Large data files (`data/`, `reports/*.csv`, `.wal/`)
- Generated files: `*.pyc`, `__pycache__/`, `.benchmarks/`, `*.egg-info/`
- IDE settings: `.vscode/`, `.idea/`

## Pre-Commit Checklist

1. Run `make ci` (lint + typecheck + test) — must pass.
2. No new `# type: ignore` without justification comment.
3. No new `noqa` without uppercase reason code (e.g., `# noqa: BLE001`).
4. Rust changes: `cargo clippy -- -D warnings` and `cargo test`.

## Git Hygiene Rules

### Worktree Discipline
- Agent worktrees (`worktree-agent-*`) MUST be cleaned up within the same session.
- `.claude/worktrees/` is gitignored; contents disposable. Max age: 24h.
- **NEVER push `worktree-agent-*` branches to remote.** Ephemeral local only.
- After parallel agent work: cherry-pick onto task branch, `git worktree prune`, delete branches.

### Branch Discipline
- One clean branch per task, created from `main`.
- No cherry-pick chains across multiple feature branches (causes commit dup + conflict hell).
- Delete local branches after merge: `git branch --merged main | grep -v main | xargs git branch -d`
- Prune remote tracking: `git fetch --prune`. Keep `main` as the only long-lived branch.

### Stash Discipline
- Max 3 stash entries. Not long-term storage — commit or discard within the session.
- Name stashes descriptively: `git stash push -m "reason"`.

### Untracked Code Audit
Before session end, `git status --short | grep "^??"` — files in `src/` or `tests/` MUST be committed, gitignored, or deleted. Never leave source/test files untracked across sessions.

### Session-End Cleanup
```bash
git stash list                    # ≤ 3 entries
git branch | wc -l                # ≤ 10 local branches
git branch | grep worktree-agent  # 0
git status --short                # clean or intentional
```

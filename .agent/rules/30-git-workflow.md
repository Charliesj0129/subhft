# Git Workflow

## Commit Messages

Use conventional commit prefixes:

- `feat:` New feature or capability
- `fix:` Bug fix
- `refactor:` Code restructuring (no behavior change)
- `perf:` Performance improvement
- `docs:` Documentation only
- `test:` Adding/fixing tests
- `chore:` Build, CI, dependencies, tooling
- `alpha:` Alpha strategy research/implementation

Examples:

- `feat(strategy): add Hawkes-process MM strategy`
- `fix(normalizer): handle one-sided LOB snapshots`
- `perf(lob): migrate compute_stats to Rust`

## Branch Strategy

- `main` is the primary branch. All development happens on `main` for this project.
- Feature branches are optional. Name: `feature/<short-description>` or `alpha/<alpha-id>`.
- Use `git stash` or worktrees (`git-parallel` skill) for context switching.

## DO NOT Commit

- `.env`, `.env.prod`, credentials
- `config/settings.py` (per-machine overrides)
- Large data files (`data/`, `reports/*.csv`, `.wal/`)
- Generated files: `*.pyc`, `__pycache__/`, `.benchmarks/`, `*.egg-info/`
- IDE settings: `.vscode/`, `.idea/`
- `node_modules/` (if any)

Verify before commit:

```bash
make lint        # Ruff lint + format check
make typecheck   # mypy
make test        # Unit tests
```

## Pre-Commit Checklist

1. Run `make ci` (lint + typecheck + test) — must pass.
2. No new `# type: ignore` without justification comment.
3. No new `noqa` without uppercase reason code (e.g., `# noqa: BLE001`).
4. Rust changes: `cargo clippy -- -D warnings` and `cargo test`.

## Git Hygiene Rules

### Worktree Discipline
- Agent worktrees (`worktree-agent-*`) MUST be cleaned up within the same session.
- `/.claude/worktrees/` is in `.gitignore` — never commit worktree artifacts.
- **NEVER push `worktree-agent-*` branches to remote.** These are ephemeral local branches only.
- After parallel agent work, cherry-pick results onto the task branch and delete worktree branches immediately.

### Branch Discipline
- One clean branch per task, created from `main`.
- No cherry-pick chains across multiple feature branches — leads to commit duplication and conflict hell.
- Delete local branches after merge. Run periodically: `git branch --merged main | grep -v main | xargs git branch -d`

### Stash Discipline
- Maximum 3 stash entries. If > 3, resolve oldest before creating new stashes.
- Stashes are NOT a long-term storage mechanism — commit or discard within the same session.
- Name stashes descriptively: `git stash push -m "reason"`.

### Session-End Cleanup
Before ending a session, verify:
```bash
git stash list                    # Should be ≤ 3 entries
git branch | wc -l                # Should be ≤ 10 local branches
git branch | grep worktree-agent  # Should be 0
git status --short                # Should be clean or intentional
```

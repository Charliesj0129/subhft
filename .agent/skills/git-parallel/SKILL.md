---
name: git-parallel
description: Manage git worktrees for parallel development — context-switch without stashing. Use when the user says "work on X while keeping Y open", starts a parallel task, or wants a long-running backtest workspace isolated from active coding.
---

# Git Parallel (Worktrees)

Lets you hold multiple branches checked out at once in sibling directories, so long-running work (backtests, large builds) does not block active coding.

## Commands

1. **List worktrees**: `git worktree list`
2. **Create parallel workspace**:
   `git worktree add ../hft_platform_hotfix hotfix/memory-leak`
3. **Remove workspace**: `git worktree remove ../hft_platform_hotfix`

## Best practice

- Create worktrees in a sibling directory (e.g., `../hft_platform_feat_X`), not inside the repo.
- Use this for blocking tasks (long-running backtests) while coding on another branch elsewhere.

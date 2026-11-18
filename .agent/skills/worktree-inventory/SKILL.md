---
name: worktree-inventory
description: "Read-only inventory of git worktrees and branches (dirty files, merged into origin/main, unpushed commits, ignored-data size, last activity) plus disk hot spots. Use for 清理工作樹/分支、git 多乾淨、C槽/D槽空間. Haiku-safe. Deletes nothing; prints candidate commands for Charlie."
---

# Worktree and disk inventory (read-only)

## Run

```bash
bash /home/charlie/hft_platform/.agent/skills/worktree-inventory/scripts/inventory.sh
```

It inventories `~/hft_platform` unless a repo path is passed as `$1`.

## Classes (the script assigns them; checked in this order)

| 類別 | 條件 | 建議 |
|---|---|---|
| GONE | directory missing (prunable) | `git worktree prune` (metadata only) |
| C 保留 | `~/hft_platform`, dirty > 0, or last commit within 3 days | keep |
| A 可移除 | merged (`yes` ancestor, or `squash` = its PR merged on GitHub), no unpushed work, ignored data excl. `.venv` < 10 MB | script prints the `worktree remove` line |
| B 需 Charlie 看 | everything else | list why |

`git worktree remove` deletes ignored files in that tree (`.env`, data, outputs),
so ignored data (excluding `.venv`) of 10 MB or more makes a tree B. `.venv`
directories are uv hard links: their apparent size frees little; never add them up.
Branch lines `squash-merged` are safe for `branch -D` only because their PR merged;
`unmerged` branches are only counted, never suggested.

Report: counts per class, the A and GONE commands, and the B rows with reasons.

## Never

Run `rm -rf`, `git worktree remove`, `git branch -D`, `prune`, `gc`, or any
`docker ... prune`. Print the commands; Charlie runs them with `!`.

---
name: worktree-inventory
description: "Read-only inventory of git worktrees and branches (dirty files, merged into origin/main, unpushed commits, ignored-data size, last activity) plus disk hot spots. Use for 清理工作樹/分支、git 多乾淨、C槽/D槽空間. Haiku-safe. Deletes nothing; prints candidate commands for Charlie."
---

# Worktree and disk inventory (read-only)

## Run

```bash
bash .agent/skills/worktree-inventory/scripts/inventory.sh
```

## Classify each worktree

| 類別 | 條件 | 建議 |
|---|---|---|
| A 可移除 | dirty=0, merged=yes, ahead=0 or no upstream, ignored < 10 MB | print `git worktree remove <path>` |
| B 需 Charlie 看 | unmerged, unpushed commits, or ignored data >= 10 MB | list why |
| C 保留 | dirty, last commit within 3 days, or `~/hft_platform` itself | keep |

`git worktree remove` deletes ignored files in that tree (`.env`, data, outputs),
which is why ignored size moves a tree to B.

## Never

Run `rm -rf`, `git worktree remove`, `git branch -D`, `prune`, `gc`, or any
`docker ... prune`. Print the commands; Charlie runs them with `!`.

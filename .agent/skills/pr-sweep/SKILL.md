---
name: pr-sweep
description: "Read-only sweep of GitHub PRs: open PRs with CI state, mergeability, failing check names, and PRs merged since a date (merged-but-maybe-not-deployed). Use for 還有紅PR嗎/哪些可以合併/CI綠了嗎/哪些merge了還沒部署. Haiku-safe. Never merges, pushes, or closes; prints the commands for Charlie."
---

# PR sweep (read-only)

## Run

```bash
python3 .agent/skills/pr-sweep/scripts/pr_sweep.py
python3 .agent/skills/pr-sweep/scripts/pr_sweep.py --merged-since 2026-11-01
```

## Report

The script prints `# | branch | CI | mergeable | red checks`. Then:

- Green and mergeable: the script prints one ready merge line per PR (with
  `--match-head-commit`). Show them for Charlie to run with `!`. Do not run them.
  `codex_review_gate` refuses merges touching `src/`, `rust_core/`, `config/`,
  `.claude/hooks/` or `.claude/settings.json` without a Codex review: say which PRs
  need one (check with `gh pr diff <N> --name-only`).
- Red: name the failing checks only. Fixing them is a separate task for a stronger model.
- Merged since a date: list them as 部署未確認. Deployment is proven only by content
  hash on the host (`.agent/rules/41-deployment.md`), which this skill does not do.

## Never

Merge, push, close PRs, or delete branches. `Charliesj0129/subhft` is public.

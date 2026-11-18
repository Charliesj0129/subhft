---
name: pr-sweep
description: "Read-only sweep of GitHub PRs: open PRs with CI state, mergeability, failing check names, and PRs merged since a date (merged-but-maybe-not-deployed). Use for 還有紅PR嗎/哪些可以合併/CI綠了嗎/哪些merge了還沒部署. Haiku-safe. Never merges, pushes, or closes; prints the commands for Charlie."
---

# PR sweep (read-only)

## Run

```bash
python3 /home/charlie/hft_platform/.agent/skills/pr-sweep/scripts/pr_sweep.py
python3 /home/charlie/hft_platform/.agent/skills/pr-sweep/scripts/pr_sweep.py --merged-since 2026-11-01
```

Run from `/home/charlie/hft_platform` (gh picks the repo from cwd). `--merged-since D`
includes day D, and dates are UTC (a merge at Taipei 00:00-08:00 shows the previous day).
Squash is this repo's merge convention.

## Report

The script prints `# | branch | CI | mergeable | red checks`. Then:

- Green and mergeable: the script prints one ready merge line per PR (with
  `--match-head-commit`). Show them for Charlie to run with `!`. Do not run them.
  `codex_review_gate` refuses merges touching `src/`, `rust_core/`, `config/`,
  `.claude/hooks/` or `.claude/settings.json` without a Codex review. The script's
  `gate` column already marks `codex` (needs review) and `protected` (touches a file
  AGENTS.md protects, e.g. `.gitignore`, `pyproject.toml`: needs Charlie's explicit OK).
- Green but mergeable `UNKNOWN`: GitHub has not computed it yet; list them, rerun later.
- Red: name the failing checks only. Fixing them is a separate task for a stronger model.
- Merged since a date: list them as 部署未確認. Deployment is proven only by content
  hash on the host (`.agent/rules/41-deployment.md`), which this skill does not do.

## Never

Merge, push, close PRs, or delete branches. `Charliesj0129/subhft` is public.

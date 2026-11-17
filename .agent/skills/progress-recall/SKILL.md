---
name: progress-recall
description: "Answer 做到哪了/計畫在哪/完成多少/還剩什麼/你在幹嘛 from files, not recall: finds the plan file, current_session.md, the PR-draft queue, open PRs and worktrees, then reports done and remaining with evidence. Haiku-safe, read-only. Not for deciding what to do next or rewriting a plan."
---

# Progress recall (read-only)

Charlie asks "做到哪了" often, especially after `/compact` or `/resume`.
Answer from files, never from the conversation alone.

## Run

```bash
bash .agent/skills/progress-recall/scripts/progress_sources.sh
```

It prints the newest plan files (`~/.claude/plans/`, Codex approved plans),
`.agent/memory/current_session.md`, the head of `~/hft_worktrees/_pr-drafts/QUEUE.md`,
open PRs, and worktrees with their last commit.

## Then

1. Pick the plan: the one the conversation named, else the newest. Say which and why,
   with its full path.
2. For each step mark 完成 / 進行中 / 未開始, each with one evidence item
   (commit, PR number, file). No evidence = 未確認, never 完成.
3. Reply as a table, then `剩餘: N 項` and the single next step the plan names.

## Escalate

Plan steps conflict with each other or with the repo, or the user wants a new plan.

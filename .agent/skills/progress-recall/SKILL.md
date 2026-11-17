---
name: progress-recall
description: "Answer 做到哪了/計畫在哪/完成多少/還剩什麼/你在幹嘛 from files, not recall: finds the plan file, current_session.md, the PR-draft queue, open PRs and worktrees, then reports done and remaining with evidence. Haiku-safe, read-only. Not for deciding what to do next or rewriting a plan."
---

# Progress recall (read-only)

Charlie asks "做到哪了" often, especially after `/compact` or `/resume`.
Answer from files, never from the conversation alone.

## Run

```bash
bash /home/charlie/hft_platform/.agent/skills/progress-recall/scripts/progress_sources.sh
```

It prints the newest plan files (`~/.claude/plans/`, Codex approved plans),
`.agent/memory/current_session.md`, the head of `~/hft_worktrees/_pr-drafts/QUEUE.md`,
open PRs, and worktrees with their last commit.

The output can exceed 40 KB: if the tool saves it to a file, read it in parts.

## Then

1. Pick the plan: the one the conversation named, else the newest. Say which and why,
   with its full path. If no plan file matches the topic, search the program's
   memory note and ADRs instead:
   `rg -il '<topic words>' ~/.claude/projects/-home-charlie-hft-platform/memory/ docs/adr/`
   and say "無 plan 檔，依據記憶檔/ADR".
   Dated headers matter: a QUEUE.md or current_session.md older than 7 days is stale;
   say so and do not rely on it alone.
2. For each step mark 完成 / 進行中 / 未開始, each with one evidence item
   (commit, PR number, file). No evidence = 未確認, never 完成.
3. Reply as a table, then `剩餘: N 項` and the next step the plan names. If the plan
   names none, write the step you infer and label it 推論.
   Evidence that exists only in a memory note is labelled 記憶來源, not 完成.

## Escalate

Plan steps conflict with each other or with the repo, or the user wants a new plan.

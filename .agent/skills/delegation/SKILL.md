---
name: delegation
description: "Decide whether to do a task directly or hand it to a subagent, and write the handoff packet, run the implement pipeline, and record the outcome. Use when a task is large enough to consider fan-out, a cheaper model, or independent review. Not for small serial edits, Tier-X work, or git."
---

# Delegation

Default is **direct**. A subagent starts cold and its output must still be
reviewed, so for small serial work delegation costs more than doing it. Tier
classification, roles, and red lines are in `AGENTS.md`; this skill does not
redefine them.

## Pick a route

| Route | When |
|---|---|
| direct | small, single-file, low-risk, or you already hold the context; always for Tier-X, review you must own, and git |
| delegate | one trigger fires: large read-only exploration to keep out of your context; bulk same-shape edits that amortize a packet; long-running test-writing or batch validation; independent adversarial review |
| fan-out | 3+ independent sub-tasks that can run in parallel, each in its own worktree |
| stop | blocked on a user decision, Tier-X confirmation, or missing input |

A task's tier is the tier of the riskiest file it must touch. Tier 3: shrink
the scope to exact functions/lines or do it yourself. Pick the cheapest
capable model: Haiku for mechanical docs and counting, Sonnet for bounded
code+test and investigation, orchestrator-class for review, routing, and git.
Before spawning, check the class scoreboard in `.agent/memory/model-routing.md`
and make sure plan mode is off (subagents inherit it and cannot edit).

## Subagent bindings

`hft-executor` (one packet), `hft-test-writer` (`tests/` only),
`hft-reviewer` (read-only, one named diff), `hft-docs` (docs only). `Explore`
for pure read-only fan-out. Use `run_in_background: false` for a single
bounded task so the report arrives with the result.

## Handoff packet

Write it when you delegate implementation. Size it to the risk: a short packet
for Tier 1 or a single-file change under ~30 lines; the full packet for
Tier 3, Do-NOT-Edit paths, multiple files, or any design choice. Templates and
the venue choice: `references/packet.md`. Before spawning, snapshot
`git status --porcelain` and compute the expected results yourself; the
executor's report is context, not evidence.

## Review and landing

Review with `strict-code-review` Step 0 (re-run the diff and the commands
yourself). Land through `git-safety`. The implement pipeline (packet ->
execute -> review -> land -> ledger) and the runtime markers that arm the
`scope_guard` hook: `references/pipeline.md`.

## Done when

The result is reviewed by you, not only reported by the executor; the
packet's verification commands pass on your own run; runtime markers are
deleted; the outcome is recorded in `.agent/memory/model-routing.md` (and the
archive file it points to) when you delegated.

## References

- `references/packet.md` — read when writing a packet or choosing the venue.
- `references/pipeline.md` — read when running packet -> executor -> reviewer -> commit, or delegating a review.

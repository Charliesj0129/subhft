---
name: bug-investigation
description: "Evidence-first root-cause investigation for unexpected behavior, test failures, or production anomalies — BEFORE proposing any fix. Adds repo-specific evidence sources (gotchas, runbooks, metrics, WAL/ClickHouse, decision traces) to generic debugging discipline."
---

# Skill: bug-investigation

## When to use
Any unexpected behavior, test failure, or production anomaly — BEFORE
proposing a fix. Complements `sequential-thinking`; this adds the
repo-specific evidence sources.

## Required inputs
Symptom description; when it started; environment (sim/live/test/host).

## Procedure
1. Reproduce or capture evidence first: failing test output, structlog lines,
   Prometheus metrics, decision traces, WAL/ClickHouse state. No evidence → say so.
2. Check known-issues: `.agent/memory/module_gotchas.md`,
   `.agent/memory/lessons_learned.md`, `.agent/memory/failed-attempts.md`,
   runbooks — many symptoms have documented causes (`HFT_ORDER_MODE=sim` fake
   fills, boot-latch, broker session races, broker-thread handoff).
3. Establish timeline: `git log` on touched files vs symptom onset.
4. Form <=3 hypotheses; for each, name the observation that would kill it;
   test cheapest-first. Verify with source reading, not recall.
5. Distinguish root cause from trigger from symptom in the writeup.
6. Do NOT fix in this skill. Report; fixing is a separate scoped task —
   a pattern-matched signal may have a different cause.

## Safety rules
Read-only toward production (guarded queries, no restarts, no config edits).
Never "test a theory" by mutating live state.

## Output format
`## Symptom` · `## Evidence` (verbatim excerpts) · `## Hypotheses & how each
was tested` · `## Root cause (or best current theory + confidence)` ·
`## Proposed fix scope` · `## Regression test that would have caught it`.

## Validation checklist
- [ ] Root cause backed by evidence, not plausibility
- [ ] Alternative hypotheses explicitly eliminated
- [ ] No state was mutated during investigation

## Example prompt
"bug-investigation: after last night's restart, subscribed_count shows 0/357
while FeedState=CONNECTED. Don't fix — find the cause."

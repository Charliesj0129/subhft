---
name: bug-investigation
description: "Evidence-first root-cause investigation of unexpected behavior, a failing test, or a production anomaly, before proposing a fix. Use when the cause is unknown, a symptom needs explaining, or a signal looks wrong. Not for applying the fix."
---

# Bug investigation

Report the cause; fixing is a separate scoped task (a pattern-matched signal
often has a different cause). Read-only toward production: guarded queries, no
restarts, no config edits, never "test a theory" on live state.

## Procedure

1. **Evidence first.** Failing test output, structlog lines, Prometheus
   metrics, decision traces, WAL and ClickHouse state (list `audit.*` tables
   before calling a cause unknowable). No evidence: say so.
2. **Known causes.** `.agent/memory/module_gotchas.md`, `failed-attempts.md`,
   `lessons_learned.md`, and runbooks already document many: `HFT_ORDER_MODE=sim`
   fakes fills and does not gate dispatch, boot-latch, broker session races,
   broker-thread handoff, registry-wide metrics reading an unset value as `0`.
3. **Timeline.** `git log` on the touched files against symptom onset.
4. **Hypotheses.** At most three; name the observation that would kill each;
   test cheapest first by reading source, not recalling it.
5. **Separate** root cause from trigger from symptom.

## Output

`## Symptom` · `## Evidence` (verbatim excerpts) · `## Hypotheses and how each
was tested` · `## Root cause (or best theory + confidence)` · `## Proposed fix
scope` · `## Regression test that would have caught it`. For a path that dies
somewhere, add an ASCII diagram marking where.

## Done when

The root cause rests on evidence rather than plausibility, alternatives are
eliminated explicitly, and nothing was mutated.

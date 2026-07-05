---
name: strict-code-review
description: "Adversarial diff review against HFT laws, boundaries, failure modes, security, and tests. Use on any diff before commit; MANDATORY (Fable/Opus reviewer) for all Tier-3 diffs and all executor-produced diffs before acceptance."
---

# Skill: strict-code-review

## When to use
Any diff before commit; ALL Tier-3 diffs (mandatory, Fable/Opus reviewer);
executor-produced diffs before the orchestrator accepts them.

## Required inputs
The diff; the originating task/packet; branch test status.

## Procedure
1. Read the packet: does the diff do exactly that — nothing more, nothing less?
2. Laws pass (hot-path files only): per-tick allocation, float price math,
   blocking IO/event-loop compute, time source (`timebase.now_ns`), FFI copies.
3. Boundary pass: broker SDK imports outside `feed_adapter/<broker>/`;
   contracts importing runtime; new import edges (`make dependency-boundary`).
4. Failure-mode pass: silent exception swallowing, fail-open paths, unbounded
   queues/maps, state machines missing transitions, non-idempotent replay.
5. Security pass: secrets, logged identifiers, injection, TLS.
6. Test pass: does a test fail if this change is reverted? Are gates/goldens/
   thresholds weakened anywhere? (Weakened gate = automatic REQUEST-CHANGES.)
7. Verify each suspected finding by reading surrounding code — no
   pattern-match-only findings.

## Safety rules
Reviewer edits nothing. Findings need concrete failure scenarios, not vibes.

## Output format
Ranked findings (severity, file:line, rule violated, failure scenario) +
verdict: APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES / ESCALATE.

## Validation checklist
- [ ] Diff-vs-packet scope checked
- [ ] All 5 passes done for applicable files
- [ ] Each finding evidence-backed
- [ ] Verdict explicit

## Example prompt
"strict-code-review this diff to order/adapter.py rate limiting; packet said
change the sliding window only. Tier 3."

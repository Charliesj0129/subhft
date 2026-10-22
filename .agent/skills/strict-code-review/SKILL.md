---
name: strict-code-review
description: "Adversarial review of a diff against the HFT laws, boundaries, failure modes, security, and tests, reporting P0-P3 findings with file:line and a verdict. Use before commit on any diff, always for Tier-3 diffs and for executor-produced diffs. Not for fixing code or for style ruff already enforces."
---

# Strict code review

Review a named diff. Report findings; do not edit.

## Step 0 — executor diffs only

The executor's report is context, never evidence. Before judging the code:

1. Scope-diff against the pre-spawn `git status --porcelain` and hashes: only
   allowed files changed; the user's dirty files untouched.
2. Re-run every packet verification command yourself.
3. Break-probe new or changed tests: break the behavior, see them fail,
   restore the file byte-exact (verify the hash).
4. Adjudicate every red gate as pre-existing or introduced, with evidence.
5. Check numeric and mechanical claims against the answer key you computed
   before spawning. Accept "identical / byte-for-byte / unchanged" only with a
   real `diff` command and its output.

## What to report

Only issues this diff introduces that the author would fix if they knew, that
are discrete and actionable, and that you can demonstrate: input or state ->
wrong result. Read the surrounding code; no pattern-match findings.

| Severity | Meaning |
|---|---|
| P0 | breaks money, safety, or data integrity; blocks release |
| P1 | likely bug or law violation on a hot, risk, or order path |
| P2 | real defect, limited blast radius |
| P3 | minor, cheap to fix |

Each finding: `[P#] file:line`, the violated rule or intent, the failure
scenario, and the evidence (code read or command output).

## What to check

- Intent: does the diff do what the packet or task asked, nothing more?
- Laws (hot-path files): per-tick allocation, float price math, blocking IO or
  >1 ms compute on the loop, time source (`timebase.now_ns`), FFI copies.
- Boundaries: broker SDK imports outside `feed_adapter/<broker>/`; contracts
  importing runtime; new import edges (`make dependency-boundary`).
- Failure modes: silent exception swallowing, fail-open paths, unbounded
  queues or maps, state machines missing a transition (HALT must still allow
  cancels), non-idempotent replay.
- Security: secrets, logged identifiers, injection, TLS.
- Tests: does a test fail if the change is reverted? Any gate, golden, or
  threshold weakened (automatic REQUEST-CHANGES)?

## Done when

You end with exactly one verdict: `APPROVE`, `APPROVE-WITH-NITS`,
`REQUEST-CHANGES`, or `ESCALATE`. With no findings, say what you checked. A
review that runs low on budget returns `REQUEST-CHANGES` or `ESCALATE` with
findings so far, never silence.

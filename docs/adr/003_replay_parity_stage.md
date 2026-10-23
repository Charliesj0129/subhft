# ADR-003: Where replay parity can be certified

## Status
Proposed (2026-10-06). No code or threshold changes with this ADR; a decision is requested.

## Context
`replay_parity` is a blocking Gate C sub-gate (`config/research/profiles/vm_ul6_strict.yaml`,
`ReplayParityGate` in `src/hft_platform/alpha/_sub_gates/replay_parity.py`). It consumes a
`replay_parity_report` attached to the backtest result and treats a missing report as a hard
failure: "cannot certify parity it never observed".

The report is produced by `src/hft_platform/replay/cli_runner.py`, which replays the strategy
and compares it with the live intents loaded from `hft.order_intents`. A candidate that has
never run live, in sim or in shadow has no rows there. So for exactly the population Gate C
exists to judge, the gate cannot pass: it can only fail on "missing report", and what it
measures for a strategy that is already live is whether the replay reproduces what the live run
did, not whether the candidate is good.

Two things follow:

1. A new candidate cannot clear Gate C by any amount of research quality. The failure is
   structural and reads as a research verdict ("blocked_by_parity" in
   `src/hft_platform/alpha/experiments.py`).
2. The gate is honest (it does not pass without evidence), but its placement is wrong: the
   evidence it needs only exists after a later stage.

Related: the gate already reports `uncovered_parity_dimensions` (e.g. `session_phase`, which the
live table does not carry), so partial coverage is an accepted state elsewhere in this gate.

## Decision (proposed)
Move the stage at which parity is blocking, without weakening it:

1. Gate C reports `replay_parity` as `not_applicable_no_live_intents` when the live side is
   empty by construction (no sim/shadow/live run exists), instead of `failed`. This is a
   different state from "report missing because the replay broke", which stays a failure.
2. `replay_parity` becomes blocking at Shadow (or Canary) exit, where live intents exist, with
   the same 95% match threshold and the same missing-report failure.
3. A candidate cannot reach Live without a passing parity report; the promotion checklist in
   `docs/runbooks/alpha-development-workflow.md` states this explicitly.

Not proposed: lowering `replay_parity_match_pct_min`, skipping parity for any candidate that has
live intents, or editing `vm_ul6_strict.yaml` (frozen).

## Consequences
- Pros: Gate C verdicts stop mixing "no live history yet" with "diverges from live"; the
  threshold keeps its meaning; parity is checked where its evidence exists.
- Cons: Gate C alone no longer implies parity, so the promotion path needs the later check to
  be wired and monitored (a gate that exists but is not run is worse than the current failure).
  Needs a new state in `SubGateResult` or an equivalent marker and matching audit/report changes.
- Follow-ups: owner decision on Shadow vs Canary as the blocking stage; a test that a missing
  report on a strategy WITH live intents still fails; an audit row showing which stage certified
  parity.

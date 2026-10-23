# ADR-004: What `min_days` means for a maker candidate judged on one contract

## Status
Proposed (2026-10-06). No code or threshold changes with this ADR; a decision is requested.

## Context
The strict profile requires `min_days: 60` for maker candidates (`vm_ul6_strict.yaml`,
`MinSampleSizeGate` in `src/hft_platform/alpha/_sub_gates/min_sample_size.py`). The maker Gate C
path (`src/hft_platform/alpha/_gate_c.py`) runs `MakerEngine.run(instrument=...)` on a single
instrument, and `n_days` comes from that run's scorecard.

A single TMF contract month is liquid for roughly a month of trading days, and no single TMF
contract in the research archive has 60 days. So for any single-contract TMF run `n_days < 60`:
`min_sample_size` cannot pass whatever the strategy does. The same shape applies to the
out-of-sample segment, which is why an OOS Sharpe over very few days is not a usable number.

The floor itself is not the problem: the evidence budget really is small (a daily-return Sharpe
over n days has a standard error of about `sqrt(252/n)`, 3.5 at 20 days). The problem is that the measurement unit (one contract) cannot reach the stated
sample, so the gate fails for a reason unrelated to the candidate.

## Decision (proposed; pick one)
A. **Rolled series.** Judge a maker candidate on a continuous front-month series with an explicit
   roll rule written into the protocol (roll date, which contract is quoted, how roll-day PnL is
   attributed). `n_days` then counts trading days across contracts. Cost: a roll rule is a modelling
   choice and must itself be pre-registered; queue and spread differ across contracts.
B. **Power-based floor.** Replace the fixed 60 with a requirement stated in detectable effect:
   `MDIC(n) <= k x break-even IC` for a stated k, where `MDIC = tanh(2.8 / sqrt(n - 3))`. A
   candidate passes the sample gate when the days available can detect an edge of the size the cost
   model says it needs. Cost: the number moves with the cost model (see the 2026-10 cost-profile
   fix), so k must be chosen once and recorded.
C. **Keep 60 and say so.** Declare that no single-contract TMF candidate can pass Gate C today and
   that the gate is a deliberate wall until more days exist. Cost: it is indistinguishable from a
   research failure in reports unless the verdict says "unreachable sample".

In every option the reported verdict must distinguish `insufficient_days` (the measurement could
not be made) from a failed edge. Thresholds elsewhere, and `vm_ul6_strict.yaml`, do not change
with this ADR.

## Consequences
- A: unlocks the verdict at the cost of a modelling decision; B: principled but moves with
  costs; C: honest but stalls the pipeline.
- Whatever is chosen, loosening must not be an accident: any option that lowers the effective bar
  needs an explicit owner sign-off recorded here.
- Follow-ups: owner decision; if A, a roll-rule protocol and tests; if B, the choice of k.

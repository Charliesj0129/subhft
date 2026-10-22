---
name: taifex-alpha-kill-criteria
description: "Pre-research feasibility gate and mandatory validation checks for TAIFEX alpha candidates (cost floor, horizon, detrended IC, bid/ask, recent data first), and how to record KILL verdicts. Use before a new alpha direction. Not for microstructure facts."
---

# TAIFEX alpha kill criteria

Prior verdicts are evidence you can re-check, not limits on what may be tried:
the KILL index is `.agent/memory/failed-attempts.md` (research section), with
artifacts under `research/experiments/` and reports. Check it before spending
effort, then decide for yourself whether a new angle changes the premise.

## Feasibility, before writing code

1. **Cost floor.** Derive the round-trip cost for the instrument now; do not
   copy a number from a file. Per-side cost = price x tax rate (charged on both
   buyer and seller for index futures) + commission range + spread crossed if a
   taker (`taifex-market-structure` has the derivation and the sources). An
   expected edge below about 2x that floor is dead on arrival.
2. **Horizon fit.** Costs weigh more as horizons shorten. Say which horizon the
   signal lives on and check the KILL index for that instrument and horizon.
3. **Alpha type.** Read the KILL index entries for the same mechanism family
   before proposing it again.

## Mandatory checks on any signal

| Check | Rule |
|---|---|
| Detrended IC | for any smoothed signal, compare IC against forward returns net of a rolling mean; a small detrended IC, an IC mostly explained by autocorrelation, or an IC that rises monotonically with horizon is trend contamination, not alpha (`feedback_detrended_ic_gate`) |
| Bid/ask execution | any edge under ~2x the median spread must be tested with entry at the ask / exit at the bid, never mid |
| Recent data first | validate on the most recent month before the full window; spread regimes shift (`feedback_backtest_recency_bias`) |
| Subsampling | if you use bars or buckets, compute IC on raw ticks too; a large ratio means boundary artifacts |
| Physics audit | check every formula's units and sign against the market mechanism (`feedback_t2_physics_audit`) |
| Multiple testing | Benjamini-Hochberg or effective test count, not a raw p-value alone |

Volume weighting adds nothing when most trades are single-lot; inventory skew
is noise at 1-lot size. Check the trade-size distribution before relying on either.

## Recording a verdict

A KILL rests on that candidate's own backtest evidence. A structural finding
from a sibling candidate can justify a watch point, not a verdict. Use the
faithful words KILL / NEEDS-MORE-DAYS / RESCUED / INCONCLUSIVE, keep the
pre-registered floors, store evidence append-only under `research/experiments/`,
and add the one-line index entry to `failed-attempts.md` through `memory-update`.

## Done when

The cost floor is derived with its inputs and date, the horizon is stated, the
KILL index was consulted, and each applicable check above has a result.

---
name: hft-mm-design
description: "Market-making design for TAIFEX (spread gate, signal layers, inventory, adverse selection) with R47 findings and evidence caveats. Use when designing or changing a maker's quoting or inventory logic. Not for SDK mechanics (hft-strategy)."
---

# Market-making design

Pattern vocabulary from the R47 maker (`strategies/r47_maker.py`). The evidence
behind it was measured on specific backtest methods and windows; treat every
number as a hypothesis to re-verify on recent data, and name the method behind
any PnL claim (`backtest_method_reliability` in memory: methods differ from
14x pessimistic to 577x optimistic). The live loop is frozen at `max_pos=1`
(`r47_tmf_v1`); do not infer a live setting from the research figures.

## Three layers

```
L1 spread gate     quote only when spread (in POINTS, not bps) clears round-trip cost
L2 signal layers   suppress / widen / skew from regime, queue, flow, imbalance signals
L3 execution       tick-grid snapping, pending tracking, gap resilience (see hft-strategy sdk.md)
```

- **L1** is the gate that mattered most: PnL tracked average spread. Compute the
  breakeven from current costs (`feedback_taifex_fee_structure`, `feedback_mini_taiex_point_value`:
  mini-TAIEX is 10 NTD per point; fees and tax are per side), not from a copy in a file.
- **L2** layers (permutation-entropy regime gate, queue-depletion suppression,
  flow-based capitulation widening, L1-imbalance skew) each defaulted to disabled
  in R47 except the imbalance skew, because ablations did not show incremental value.
  Enable one layer at a time and measure incremental PnL with the unified
  backtest framework; if it does not help, leave it off.
- **L3** details are in `hft-strategy` (`references/sdk.md`).

## Design rules

1. Start with the spread gate only; add one layer at a time with evidence.
2. Backtest with the method that models queue position and bid/ask fills; edge
   below 2x spread must use bid/ask fills. Do not rely on one engine default.
3. Quote age must be bounded: unselected making is negative.
4. Fixed minimal inventory skew beat academic models on this 1-point-tick CLOB
   at 1-lot size; revisit only with evidence.
5. A circuit breaker that cuts losses early can remove the recovery that produces
   the profit; analyze drawdown-then-recovery paths before adding one.
6. Fresh quotes over stale ones: queue priority did not outweigh information decay.
7. Shadow for at least one full session before any live consideration, and
   remember the freeze.

## Done when

The design states its breakeven spread with the cost inputs and date, each
enabled layer has measured incremental PnL by a named method, and shadow
evidence exists before any promotion discussion.

## References

- `references/r47-evidence.md` — read to see the original R47 ablation findings and configuration template, with their caveats.

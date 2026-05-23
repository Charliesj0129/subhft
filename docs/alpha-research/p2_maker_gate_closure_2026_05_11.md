# P2 Maker Execution Gate — Closure and Next Validation (2026-05-11)

**Lane**: R65 Pivot 2 — Execution / Marketability Model
**Status**: **PROMOTE as infrastructure; KILL as standalone alpha**
**Decision**: Freeze P2 as a maker execution gate, not an alpha discovery lane.

## Final Verdict

P2 does not discover a standalone maker alpha. The stable component is a
geometric maker-quality gate: `wide_spread x likely_to_fill`. A one-feature
score, `p_fill_hat x spread_z`, retains 82-93% of the full 7-feature
regression composite on most side/horizon pairs, while depth, OFI, queue,
volatility, churn, and toxicity add marginal or noisy contribution.

The result is therefore infrastructure-positive but alpha-negative. P2 should
be promoted as a maker execution gate and used downstream to filter entries
from external-driver or directional models. It should not be traded standalone.

Chinese summary:

```text
P2 沒有找到可獨立交易的 maker alpha。穩定訊號本質上是
wide_spread x likely_to_fill 的幾何型 maker-quality gate。單特徵
score = p_fill_hat x spread_z 已保留完整 7-feature regression composite
的大部分效果，而 depth、OFI、queue、vol、churn、toxicity 等六個微結構
特徵只提供邊際或噪音貢獻。

因此 P2 的結論是 infrastructure-positive but alpha-negative。它應被升級為
maker execution gate，供後續 external-driver 或 directional model 過濾掛單
進場，不應作為 standalone alpha 交易。
```

## What P2 Answers

P2 answers an execution question:

```text
In this book state, does a passive maker order have enough spread cushion,
and is fill/markout quality acceptable enough to allow placement?
```

It does not answer a directional-alpha question:

```text
Will price move up or down enough to justify taking a side?
```

The correct architecture is:

```text
direction model -> chooses side
P2 gate         -> permits or blocks maker entry on that side
```

not:

```text
P2 -> chooses direction and trades standalone
```

## Evidence Trail

1. Initial composite EV (`composite_ev.py`) showed buy-side top-vs-bottom raw
   EV separation of +0.68 to +1.19 pt, while sell-side appeared flat.
2. Stability audit (`stability_audit.py`) found R65-style single-day
   dominance: 2026-04-02 carried 64-75% of the headline. Per-day median
   durable signal was much smaller: about +0.05 to +0.09 pt buy and +0.02 to
   +0.04 pt sell.
3. Continuous markout regression (`markout_regression.py`) replaced the
   binary adverse-threshold target. Sell-side asymmetry closed: top-vs-bottom
   separation became +0.68 to +1.65 pt, with both sides positive across all
   horizons.
4. Regression-composite stability passed all 6 side/horizon combinations:
   `sign_consistency = 1.0000`, `max_single_day_share = 0.12-0.29`, under the
   R65 gate of <= 0.4.
5. TXFD6 cross-validation passed the same pattern. TXF was structurally
   stronger than TMF, with pearson >0.6 vs roughly 0.2 for TMF. Buy-side
   narrow-spread TXF cohorts were negative; only wide-spread q4 carried
   signal.
6. Cross-instrument transfer passed 5/6 practical transfers. TMF->TXF retained
   >=80% separation in all 4 checked transfers; TXF->TMF sell retained 82-84%;
   TXF->TMF buy failed.
7. Feature decomposition was decisive: `spread_pt` dominated trained weights.
   `p_fill_hat x spread_z` retained 82-93% of the full regression composite in
   most cases and beat the full model on TXF buy h=5000.

Durable artifacts:

- `outputs/p2_exec_predictor/tmf/markout_regression/REPORT.md`
- `outputs/p2_exec_predictor/tmf/stability_audit_regression/REPORT.md`
- `outputs/p2_exec_predictor/txf/markout_regression/REPORT.md`
- `outputs/p2_exec_predictor/txf/stability_audit_regression/REPORT.md`
- `outputs/p2_exec_predictor/cross/tmf_to_txf/REPORT.md`
- `outputs/p2_exec_predictor/cross/txf_to_tmf/REPORT.md`
- `docs/alpha-research/p2_exec_predictor_smoke_2026_05_11.md`

## Relation to R65 / F1-C

P2 explains why R65 Regime 11 looked like a PROMOTE:

```text
Regime 11 = wide-spread maker cohort
```

R65 failed because the regime-level simulator and split were unsafe:

```text
fill / markout simulator not executable enough
OOS cohort flip
maker_bid fill priced at original bid
30s forward mid, not executable exit
```

P2 reframes the useful part:

```text
Not: Regime 11 is alpha.
But: wide spread + likely fill has maker-quality gating value.
```

That is a cleaner and more durable claim.

## Non-Goals

Do not:

```text
trade P2 standalone
claim R65 Regime 11 is revived
add more microstructure feature reskins to force alpha
make the 7-feature regression the primary deployable model
use toxicity / OFI / depth as the next reskin direction
```

The feature decomposition says those surfaces are mostly overfit risk.

## P2-V: Maker Gate Validation

Open a smaller validation lane named:

```text
P2-V: Maker Gate Validation
```

Goal:

```text
Validate whether p_fill_hat x spread_z reliably improves maker execution
quality for future directional strategies.
```

## P2-V Smoke Execution (2026-05-11)

Implemented a frozen simple-gate validator:

```text
research/experiments/p2_exec_predictor/simple_gate_validation.py
```

It does not train new models. It loads existing fill and markout-regression
models, then compares:

```text
simple = p_fill_hat x spread_z
full   = p_fill_hat x pred_markout
```

Outputs:

```text
outputs/p2_exec_predictor/tmf/simple_gate_validation/REPORT.md
outputs/p2_exec_predictor/txf/simple_gate_validation/REPORT.md
```

### TMF Result

TMF confirms the simple gate is useful but threshold-sensitive:

```text
top 10% simple retention:
buy  h500  = 0.78
buy  h2000 = 0.80
buy  h5000 = 0.84
sell h500  = 0.81
sell h2000 = 0.80
sell h5000 = 0.80
```

All TMF top-10 gates have positive pass-minus-fail raw EV. Sign consistency is
1.0 for buy and 0.8 for sell h5000. Single-day share improves as the gate gets
stricter; buy h500 remains slightly above the R65-style 0.4 line at 0.4477,
while buy h2000/h5000 and all sell top-10 gates are <=0.4.

Interpretation:

```text
TMF simple gate is viable at strict top 10%.
Loose top 30% and medium top 20% are weaker and sometimes too single-day-heavy.
```

### TXF Result

TXF strongly validates the simple gate:

```text
top 10% simple retention:
buy  h500  = 1.03
buy  h2000 = 1.07
buy  h5000 = 1.86
sell h500  = 0.89
sell h2000 = 0.95
sell h5000 = 0.95
```

All TXF simple gates have sign consistency 1.0. All top-10 single-day shares
are <=0.3179. The simple score beats the full model on TXF buy, especially
h5000, which confirms that the extra microstructure features were adding
overfit rather than durable signal in that slice.

### P2-V Smoke Decision

```text
P2-V simple gate: PASS for infrastructure.
Recommended frozen gate for next integration: strict top 10%.
Medium top 20% can be retained as WATCH for sizing experiments.
Loose top 30% should not be promoted without a separate stability reason.
```

This strengthens the final P2 verdict:

```text
PROMOTE as maker execution-gate infrastructure.
KILL as standalone alpha.
```

### Reproducibility Record

Run commands:

```bash
uv run python -m research.experiments.p2_exec_predictor.simple_gate_validation \
  --synth-dir research/data/derived/p2_fill_events_tmf_smoke \
  --src-out outputs/p2_exec_predictor/tmf \
  --out outputs/p2_exec_predictor/tmf

uv run python -m research.experiments.p2_exec_predictor.simple_gate_validation \
  --synth-dir research/data/derived/p2_fill_events_txf_smoke \
  --src-out outputs/p2_exec_predictor/txf \
  --out outputs/p2_exec_predictor/txf
```

Ignored local artifacts and hashes:

```text
6c883bb6f0a62455e9a5aae3f95d6bcfd7103e58c031dfb624c47b23693b9c6d  outputs/p2_exec_predictor/tmf/simple_gate_validation/REPORT.md
3b362feef624cc840473d4d5682e50991019193a1c53b332c053d269d065760a  outputs/p2_exec_predictor/tmf/simple_gate_validation/summary.json
2c30013fa0b7e12b3b25a76a692bd82afd21b9236758164eb1c4a1f0b5b9461a  outputs/p2_exec_predictor/txf/simple_gate_validation/REPORT.md
60947536a9fd7074c6a194a9e8be33f4861dc3a0349169d4bc14d8f2fde204a6  outputs/p2_exec_predictor/txf/simple_gate_validation/summary.json
```

These outputs are intentionally not committed because `outputs/` is an
artifact directory.

## Infrastructure Contract

Stable P2 maker gate contract:

```text
inputs:
  p_fill_hat: float
  spread_z: float

score:
  maker_quality_score = p_fill_hat x spread_z

gate:
  strict_top_10%

outputs:
  maker_allowed: bool
  maker_quality_score: float
```

Side usage:

```text
external long bias  -> evaluate maker_bid gate
external short bias -> evaluate maker_ask gate
```

P2 must not choose direction. It only permits or blocks passive entry on the
side chosen by a separate directional model.

### Step 1: Freeze Simple Gate

Freeze the simple gate first:

```text
score = p_fill_hat x spread_z
```

Evaluate three fixed thresholds:

```text
loose gate: top 30%
medium gate: top 20%
strict gate: top 10%
```

Do not retrain a new complex model for this step.

### Step 2: Gate-Only Audit

Do not score directional alpha. Score maker action quality:

```text
p_fill
conditional markout after fill
unconditional EV
adverse selection rate
spread captured
cancel / no-fill rate
daily stability
single-day dominance
cross-instrument retention
```

Core question:

```text
Do maker orders that pass the gate have better execution quality than maker
orders that fail it?
```

### Step 3: Simple vs Full Comparison

Compare:

```text
simple_gate = p_fill_hat x spread_z
full_gate   = 7-feature regression composite
```

Criteria:

```text
EV separation
daily median separation
single-day dominance
cross-instrument transfer
OOS stability
implementation simplicity
```

If the simple gate keeps >=80% of full-model effect, use the simple gate.
Keep the full regression composite as a research artifact only.

### Step 4: Plug Into F2, Not Standalone

P2 becomes execution layer infrastructure:

```text
external alpha says: long bias  -> P2 says maker_bid allowed or blocked
external alpha says: short bias -> P2 says maker_ask allowed or blocked
```

The main research lane remains:

```text
F2-A: External Driver Opportunity Audit
```

Priority external drivers:

```text
1. TXF / TMF / MTX same-family futures lead-lag
2. Spot proxy / ETF / weighted stock basket
3. Overseas index futures, especially night session
4. Options IV / skew
```

## Final Decision

```text
P2:
    PROMOTE as infrastructure
    KILL as standalone alpha

Next:
    Freeze simple p_fill_hat x spread_z gate
    Run P2-V maker gate validation
    Plug P2 into F2 external-driver audit
```

## Final P2-V Verdict

P2 is promoted as maker execution-gate infrastructure and killed as standalone
alpha.

The deployable form is the strict top 10% simple gate:

```text
score = p_fill_hat x spread_z
```

TMF strict-gate retention is approximately 0.78-0.84, mostly reaching or near
the 80% threshold. TXF strict-gate retention is approximately 0.89-1.86 and
passes stability checks, with single-day share below 0.4. Loose and medium
gates are weaker and should not be used as the default deployment gate.

The full 7-feature regression composite is retained as research evidence, but
the simple spread-fill gate is the preferred production-facing infrastructure
component.

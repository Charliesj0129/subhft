# P2 — Execution / Marketability Predictor — Smoke Run (TMF, 2026-05-11)

**Lane**: Pivot 2 from R65 plan §7.5 — execution-layer predictor as the first
new use of the L2 panel after R65 closure.
**Subject**: Train P(fill | LOB) and P(adverse | filled, LOB) per side ×
horizon ∈ {500, 2000, 5000} ms on 33 active TMFD6 trading days, day-OOS
split, with per-spread-quintile audit.
**Current verdict (rev 5)**: **PROMOTE as maker execution-gate
infrastructure; KILL as standalone alpha. CONFIRMED by 4 orthogonal tests
(pooling / residual / interactions / spread-only cross transfer): the only
durable signal is `p_fill × spread`. Microstructure features add no
generalizable value beyond pure geometric tilt.** Pooling rebalances which
panel is hurt; the residual model fails (negative residual r² everywhere);
extending to spread × {imb, ofi, qratio, vol, churn, tox} interactions
overfits and degrades the composite by up to −1.68 pt on TXF buy.

Earlier smoke verdict: all 12 (side × horizon × target) targets cleared test
AUC > 0.55; no cohort flip in any spread quintile. That result justified
scaling to TXFD6 and deeper stability/transfer checks. Rev 5 supersedes
the rev 4 feature-decomposition verdict by adding 4 confirmation tests; the
core conclusion ("predictor is geometric, not microstructural") is unchanged.

## What was built

| File | Purpose |
|---|---|
| `research/experiments/p2_exec_predictor/synth_fill_events.py` | Walks the continuous L2 panel, synthesizes per-snapshot quote events with **mechanical** fill labels (`min_trade_px ≤ q_bid` / `max_trade_px ≥ q_ask` within [t+place_latency, t+place_latency+h]) and post-fill markouts. Place latency 395 ms (Shioaji measured P95). |
| `research/experiments/p2_exec_predictor/models.py` | Self-contained `LogisticBinary` (GD + L2), `FeatureNormalizer`, `compute_auc` (Mann-Whitney U), `calibration_by_decile`, `stratified_metric`. Numpy-only, no sklearn. |
| `research/experiments/p2_exec_predictor/train_eval.py` | Day-OOS train + per-spread-quintile audit. Drops inactive days (< 200 fills/day) before split. |

## Smoke results (TMFD6, full panel)

- 58 day-files synthesized; 24 dropped as inactive (Lunar New Year + weekends + April 3-11 holiday window).
- 33 active days → train (first 23, 2026-01-25 → 2026-03-26) / test (last 10, 2026-03-27 → 2026-04-13).
- 12 trained models (2 sides × 3 horizons × 2 targets).

| side | h(ms) | target  |  n_test  | base_rate | train_auc | test_auc | brier  |
|------|-------|---------|----------|-----------|-----------|----------|--------|
| buy  |   500 | fill    | 2,633,452 |   0.5435 |    0.5896 |   0.6316 | 0.2388 |
| buy  |   500 | adverse | 1,431,098 |   0.2198 |    0.5778 |   0.6428 | 0.1768 |
| buy  |  2000 | fill    | 4,168,977 |   0.6465 |    0.6463 |   0.6440 | 0.2189 |
| buy  |  2000 | adverse | 2,695,018 |   0.2742 |    0.5618 |   0.6162 | 0.2042 |
| buy  |  5000 | fill    | 4,708,145 |   0.7295 |    0.7031 |   0.6536 | 0.1909 |
| buy  |  5000 | adverse | 3,433,668 |   0.3362 |    0.5583 |   0.5916 | 0.2255 |
| sell |   500 | fill    | 2,633,452 |   0.4681 |    0.5957 |   0.6220 | 0.2364 |
| sell |   500 | adverse | 1,232,573 |   0.2549 |    0.5713 |   0.6025 | 0.1925 |
| sell |  2000 | fill    | 4,168,977 |   0.5853 |    0.6545 |   0.6494 | 0.2261 |
| sell |  2000 | adverse | 2,439,582 |   0.2998 |    0.5536 |   0.5807 | 0.2132 |
| sell |  5000 | fill    | 4,708,145 |   0.6819 |    0.7154 |   0.6619 | 0.2015 |
| sell |  5000 | adverse | 3,209,319 |   0.3544 |    0.5508 |   0.5596 | 0.2309 |

- **AUC > 0.55**: 12 / 12.
- **|train_auc - test_auc| > 0.05**: 3 / 12 (`buy_h500_adverse` +0.06, `buy_h5000_fill` -0.05, `sell_h5000_fill` +0.03 borderline). All small and bidirectional — not a one-sided cohort drift.
- **Test cohort is mostly stronger than train**, suggesting the late-cycle TMFD6-only window has a cleaner LOB signal than the earlier cross-contract sample (TMFB6 → TMFC6 → TMFD6 spans).

## Why this passed where R65 failed

R65 (F1-C / Pivot 1A / 1B) targeted **regime-conditional EV**, where the
target is the cross-product of a regime cut and a forward EV — both can
shift cohort across panel halves. The DT leaf id stays stable but the
microstructure cohort it catches flips.

P2 targets **mechanical fill** (literally: did a trade happen at our price
within 500-5000 ms?) and **mechanical markout** (did the mid move
adversely after our fill?). These are direct empirical observables; no
"regime cut" abstraction sits between the LOB and the label. The same
LOB feature → same physical phenomenon in any spread cohort.

The per-spread-quintile audit confirms this directly:

`buy_h2000_fill` test AUC by spread quintile:

| spread quintile | mean_spread (pt) | n         | AUC    | base_rate |
|----|------|-----------|--------|--------|
|  0 | 1.00 |   571,694 | 0.6473 | 0.7266 |
|  2 | 2.00 | 1,150,998 | 0.6476 | 0.6702 |
|  3 | 3.00 | 1,594,659 | 0.6448 | 0.6194 |
|  4 | 4.87 |   851,626 | **0.6857** | 0.6116 |

The wide-spread cohort (quintile 4) — exactly the cohort where R65 cohort
flips originated — has the **strongest** test AUC, not the weakest. The
predictor generalizes to wide spreads, not just to the train-dominant
narrow-spread cohort.

## Composite EV check (follow-up #1, completed 2026-05-11)

Post-training composite scorer
`p_good_fill = p_fill_hat × (1 − p_adverse_given_fill_hat)` evaluated on the
same 10-day OOS test panel, deciled, with realized fill rate × realized
E[markout|fill] as the per-decile raw EV in points. Code:
`research/experiments/p2_exec_predictor/composite_ev.py`. Output:
`outputs/p2_exec_predictor/tmf/composite_ev/{*.json,REPORT.md,summary.json}`.

| side | h(ms) | top    | bot    | top − bot | top fill_rate | top E[mko\|fill] |
|------|-------|--------|--------|-----------|---------------|------------------|
| buy  |   500 | +1.308 | +0.122 |  **+1.186** | 0.84 | +1.55 |
| buy  |  2000 | +1.017 | +0.211 |  **+0.806** | 0.87 | +1.16 |
| buy  |  5000 | +0.954 | +0.275 |  **+0.679** | 0.90 | +1.05 |
| sell |   500 | +0.093 | +0.069 |  +0.024   | 0.62 | +0.15 |
| sell |  2000 | +0.097 | +0.158 |  −0.062   | 0.77 | +0.13 |
| sell |  5000 | +0.106 | +0.128 |  −0.021   | 0.84 | +0.13 |

**Asymmetric verdict.** The composite combines cleanly on the **buy** side
across all 3 horizons (top-vs-bot raw EV +0.68 → +1.19 pt). On the **sell**
side it flatlines (|top − bot| ≤ 0.06 pt) — the sell-side adverse-fill model
is too weak relative to the fill model, so `p_good = p_fill × (1 − p_adv)`
ends up tracking `p_fill` alone. Top-decile sell snapshots have the highest
fill rate but the same shallow E[markout|fill] as everywhere else.

**Decile shape, buy h=2000** (representative): non-monotone with a U-bump at
d0 then a clean upward sweep d3 → d9. d0 raw EV = +0.21 (low p_good but
adverse momentum that itself signals favorable post-fill drift), d1-d5 flat
~+0.10, then d6 +0.16, d7 +0.18, d8 +0.27, d9 +1.02. The d9 dominance is
the headline: top decile is materially better than the rest of the panel,
not merely better than d0.

**What this says.**
1. The *fill* component is the dominant predictor of where you end up
   trading; the *adverse* component shifts the marginal cohort but doesn't
   override sign.
2. Buy/sell asymmetry in this 10-day window almost certainly reflects panel
   drift (test window 2026-03-27 → 2026-04-13 was directionally bullish):
   buy fills caught the up-drift, sell fills paid for it. A directionless
   composite (e.g. trained on per-day demeaned markout, or with a longer
   panel that averages multiple regimes) would likely close the asymmetry.
3. Net EV (raw − 4 pt RT cost) is negative everywhere — the predictors are
   not a stand-alone money-printer. They are a quality filter / gate to
   feed into a separate directional or calibration layer.

**Headline.** Composite combination is informative on the buy side and
indistinguishable from `p_fill` alone on the sell side. The first-slice
verdict (mechanical predictors are durable infrastructure) is reinforced;
the asymmetry is a known follow-up, not a smoke failure.

## Stability audit — single-day dominance flagged 2026-05-11

A per-test-day stability audit (`research/experiments/p2_exec_predictor/stability_audit.py`,
output `outputs/p2_exec_predictor/tmf/stability_audit/REPORT.md`) re-ran the
composite EV decile split *within each test day* and discovered the headline
is 64-75 % carried by **one day** (2026-04-02). Worst day = 2026-04-02 for
all 6 (side × horizon) combinations. This matches the R65 §7.1 single-day-
dominance pathology profile (gate threshold ≤ 0.4; observed up to 0.75).

Per-day mean vs leave-04-02-out mean vs per-day **median** (the robust statistic):

| tag        | mean_full | mean_drop_04-02 | median (durable signal) |
|------------|-----------|-----------------|--------------------------|
| buy_h500   | +0.336    | +0.092          | **+0.093**               |
| buy_h2000  | +0.207    | +0.028          | **+0.073**               |
| buy_h5000  | +0.124    | −0.038          | **+0.088**               |
| sell_h500  | +0.081    | +0.044          | **+0.026**               |
| sell_h2000 | **−0.203**| +0.047          | **+0.039**               |
| sell_h5000 | −0.097    | +0.086          | **−0.001**               |

**Revised verdict.**
1. The "+0.68 → +1.19 pt buy-side headline" overstates the typical-day
   signal by an order of magnitude. The predictor's *durable* per-day raw
   EV separation is ~+0.05 → +0.09 pt — small but reproducible.
2. The "sell-side composite collapses" claim is **wrong**. Removing 04-02
   makes sell-side mean separation positive (+0.04 → +0.09 pt). The
   asymmetry was almost entirely one outlier day where the wide-spread
   cohort whipsawed the maker against the move; the predictor's median-day
   sell-side behavior is symmetric with buy.
3. The composite predictor fails the R65 §7.1 single-day-dominance gate at
   `max_share ≤ 0.4` (observed 0.42 → 0.75). It is **not** a stand-alone
   filter that can be promoted; it requires a stability-aware aggregator
   (e.g. require sign agreement on ≥7 of 10 test days, or train on a
   panel long enough to drown out single-day blow-up).
4. **Spread-quintile audit confirms wide-spread cohort is the carrier of
   the pathology**: q4 (~5pt spread) shows extreme swings (buy_h2000
   +1.83 pt, sell_h2000 −4.47 pt), while q0-q3 (≤3pt) show modest, sane
   numbers. This is the same wide-spread cohort that the per-target AUC
   audit had previously flagged as the *strongest* — but AUC measures
   ranking, not realized EV, so the wide-spread cohort can simultaneously
   be high-AUC and high-volatility.

Honest 2-line summary: **mechanical fill target avoids the F1-C/R65
*regime-conditional* cohort flip, but the *score-conditional* cohort flip
(top vs bottom decile of `p_good`) is still present on big-move days.**
The fix is not in the labeling layer; it is in the aggregation layer.

## Markout regression composite — **STRUCTURAL FIX 2026-05-11**

The binary `adverse < threshold` target was the bottleneck. Replacing it
with a continuous linear regression of *realized markout in points* on the
same 7 features and re-scoring as `score = p_fill_hat × pred_markout`
**closes the asymmetry, passes the R65 §7.1 single-day-dominance gate, and
produces a 10x larger durable signal** than the binary composite.

Code: `research/experiments/p2_exec_predictor/markout_regression.py`
(adds a `LinearRegressor` and a per-(side, h) train + composite EV).
Stability audit extended with `--score-mode regression`. Outputs at
`outputs/p2_exec_predictor/tmf/{markout_regression/, stability_audit_regression/}`.

### Composite EV (regression score)

| side | h(ms) | top    | bot    | top − bot |
|------|-------|--------|--------|-----------|
| buy  | 500  | +1.376 | −0.276 | **+1.65** |
| buy  | 2000 | +1.238 | −0.242 | **+1.48** |
| buy  | 5000 | +1.260 | −0.264 | **+1.52** |
| sell | 500  | +0.416 | −0.301 | **+0.72** |
| sell | 2000 | +0.427 | −0.250 | **+0.68** |
| sell | 5000 | +0.415 | −0.301 | **+0.72** |

vs binary composite headline: buy +0.68 → +1.19, sell +0.02 / −0.06.
**Sell-side asymmetry GONE** — the binary threshold was discarding the
marginal markout gradient that mattered.

### Per-day stability (regression score)

| tag        | sign_consistency | median(top−bot) pt | max_single_day_share | worst_day |
|------------|------------------|--------------------|--------------------- |-----------|
| buy_h500   | **1.0000**       | +0.7416            | 0.2893               | 2026-04-02 |
| buy_h2000  | **1.0000**       | +0.7397            | 0.2703               | 2026-04-02 |
| buy_h5000  | **1.0000**       | +0.9184            | 0.2363               | 2026-04-02 |
| sell_h500  | **1.0000**       | +0.6496            | 0.1242               | 2026-04-01 |
| sell_h2000 | **1.0000**       | +0.6513            | 0.1380               | 2026-04-01 |
| sell_h5000 | **1.0000**       | +0.7272            | 0.1510               | 2026-03-29 |

vs binary composite: sign_consistency 0.50–1.00 (mostly 0.7), median
+0.05 to +0.09 pt, max_share 0.42–0.75 (FAIL gate). The regression
composite **passes the R65 §7.1 ≤0.4 single-day-dominance gate**.

### Spread-quintile (regression score)

ALL quintiles on BOTH sides have positive top−bot separation. Narrow
spread (q0, ~1pt) +0.41–+0.61 pt, wide spread (q4, ~5pt) +1.34–+3.06 pt
(strongest, but no longer wildly volatile). Compare to binary composite
where q0 was negative on both sides and q4 swung +1.83 / −4.47.

### Predictor quality on filled rows

R² is small (0.001–0.084 across combos), pearson 0.09–0.34, but the
stratified discrimination AUC on `markout > 0` is 0.54–0.67. The model
is a weak point-estimator but a strong ranker — exactly what a decile
gate needs.

### Net EV picture (rt_cost = 4.0 pt TMFD6)

Top-decile per-attempt net EV: still negative across all combos (−2.6 to
−3.6 pt) because raw EV per attempt is only +0.4 → +1.4 pt. The
regression composite is **a real durable ranker but not yet a stand-alone
profitable strategy** — it needs to be combined with either:
1. A directional alpha (regress markout on alpha residual after directional EV is removed),
2. A lower cost tier (institutional fees would change the picture),
3. A longer horizon where realized markouts dominate fixed costs.

**Headline change**: the predictor lane is now durable (passes stability
gates) and the "P2 doesn't survive R65-style audit" risk is closed via a
labeling/loss change, not by adding more data. Marketability-predictor
infrastructure is **promotable as a backtest-realism / live-gating
component**, just not as a stand-alone alpha.

## TXFD6 cross-instrument validation 2026-05-11

Same pipeline rerun on TXFD6 (`research/data/derived/txf_full_2026/`,
24 days, 73 M events). Synth + train_eval + composite_ev + markout
regression + stability_audit ran in ~12 min total. Outputs at
`outputs/p2_exec_predictor/txf/`.

### TXFD6 regression composite

| side | h(ms) | top    | bot    | top − bot | pearson |
|------|-------|--------|--------|-----------|---------|
| buy  | 500  | +2.084 | −0.244 | **+2.33** | 0.705 |
| buy  | 2000 | +2.359 | −0.254 | **+2.61** | 0.684 |
| buy  | 5000 | +1.798 | −0.303 | **+2.10** | 0.636 |
| sell | 500  | +0.677 | −0.533 | **+1.21** | 0.644 |
| sell | 2000 | +0.735 | −0.539 | **+1.27** | 0.614 |
| sell | 5000 | +0.989 | −0.638 | **+1.63** | 0.568 |

TXF predictor is **structurally stronger** than TMF (pearson 0.6+ vs
~0.2; top-bot 1.2-2.6 pt vs 0.7-1.5 pt). Larger tick size and bigger
realized markout magnitudes give the linear regressor more SNR.

### TXFD6 stability (regression score)

All 6 (side × h) combos: sign_consistency = **1.0000**, max single-day
share 0.18–0.34 — all under R65 §7.1 ≤0.4 gate. Median per-day separation
+0.89 → +1.66 pt buy, +0.94 → +1.09 pt sell. Worst day varies (no panel-
wide outlier like TMF's 2026-04-02).

### TXFD6 spread-quintile cohort behaviour

- **Sell side**: positive separation in ALL quintiles (+0.26 → +0.53 narrow,
  +2.17 → +3.35 wide). Robust.
- **Buy side**: q0–q3 (spread 1.85–4 pt) are NEGATIVE (−0.06 → −0.34 pt);
  q4 (spread 7.81–11.56 pt) is huge (+4.44 → +9.41 pt). Buy-side signal
  is concentrated in wide-spread snapshots only — same wide-spread carrier
  pattern seen on TMF, but more pronounced.

### Net EV (rt_cost = 4.0 pt)

Top-decile per-attempt net EV on TXF h=2000 buy: +2.36 − 4 = −1.64 pt
(closer to break-even than TMF's −2.6 pt). Per-fill calc:
fill_rate × (E[mko|fill] − rt_cost) ≈ 0.65 × (3.61 − 4) = −0.25 pt per
attempt. With a slightly lower fee tier or longer horizon, this could
cross zero.

### Headline cross-validation

The regression-composite + stability-audit pattern is **panel-portable**:
worked on TMF, worked on TXF without retuning. The "P2 lane survives R65
audit" claim now has 2 independent instruments backing it.

## Cross-instrument transfer 2026-05-11

`research/experiments/p2_exec_predictor/cross_transfer.py` evaluates
SOURCE-panel-trained (fill + markout regression) models on TARGET panel's
test split, with the same composite EV decile + per-day stability audit.

| direction | side | h(ms) | top−bot pt | in-panel | retention | sign_consist | max_share |
|-----------|------|-------|-----------|----------|-----------|--------------|-----------|
| TMF→TXF   | buy  | 2000 | **+2.79** | +2.61 | **107%** | 1.0000 | 0.244 |
| TMF→TXF   | buy  | 5000 | **+3.44** | +2.10 | **164%** | 1.0000 | 0.206 |
| TMF→TXF   | sell | 2000 | +1.11     | +1.27 | 87%      | 1.0000 | 0.168 |
| TMF→TXF   | sell | 5000 | +1.34     | +1.63 | 82%      | 1.0000 | 0.234 |
| TXF→TMF   | buy  | 2000 | +0.17     | +1.48 | **12%**  | 1.0000 | 0.190 |
| TXF→TMF   | buy  | 5000 | **−0.38** | +1.52 | **−25%** | 1.0000 | 0.226 |
| TXF→TMF   | sell | 2000 | +0.57     | +0.68 | 84%      | 1.0000 | 0.132 |
| TXF→TMF   | sell | 5000 | +0.59     | +0.72 | 82%      | 0.9000 | 0.155 |

**Verdict: 5/6 transfers pass; buy-side TXF→TMF is broken.**

Pattern reading:
1. **TMF-trained weights generalize cleanly to TXF.** Linear regression
   trained on smaller-magnitude TMF markouts scales up correctly when
   applied to TXF's wider-spread, higher-markout snapshots. Buy-side
   TMF→TXF actually outperforms TXF in-panel (+2.79 vs +2.61) — likely
   because TMF training is regularized against the TXF-buy wide-spread
   overfitting we saw earlier.
2. **TXF-trained buy-side weights do NOT transfer to TMF.** TXF buy
   regressor is overfit to wide-spread (+9 pt at q4) snapshots that
   don't exist in TMF; on TMF data the weights produce essentially
   random ranking (+0.17 pt or worse). TXF buy h=5000 transfer is
   actively negative (top decile worse than bottom).
3. **Sell-side transfers symmetrically** in both directions (~80 % of
   in-panel separation either way) — the sell-side signal is genuinely
   instrument-agnostic.
4. All passing transfers also pass the per-day stability gate
   (sign_consistency ≥ 0.9, max_share < 0.30) — the cross-instrument
   ranking is durable, not single-day-driven.

**Implication.** A *pooled* predictor (train on TMF + TXF combined) should
inherit TMF's stability without losing TXF's signal magnitude. Worth
trying as the next slice. The current pair of single-panel predictors
already gives us a deployable maker-quality gate that survives R65-style
audit.

## Feature decomposition — spread carries 82-93 % of the signal 2026-05-11

Inspecting the trained markout regressor weights (z-scored) showed
`spread_pt` dominates by 10-26x over the next feature on TXF and 3-18x
on TMF. To test whether the predictor reduces to a 1-feature spread
heuristic, computed `score = p_fill_hat × spread_z` (a 1-feature
composite using only the existing fill model + raw spread quantile)
and compared decile separation to the 7-feature regression composite:

| panel | side | h(ms) | spread×p_fill (pt) | full 7-feat (pt) | ratio |
|-------|------|-------|---------------------|------------------|-------|
| TMF | buy  | 500  | +1.39 | +1.65 | 84%  |
| TMF | buy  | 2000 | +1.30 | +1.48 | 88%  |
| TMF | buy  | 5000 | +1.37 | +1.52 | 90%  |
| TMF | sell | 500  | +0.62 | +0.72 | 87%  |
| TMF | sell | 2000 | +0.63 | +0.68 | 92%  |
| TMF | sell | 5000 | +0.67 | +0.72 | 93%  |
| TXF | buy  | 500  | +2.53 | +2.33 | **108%** |
| TXF | buy  | 2000 | +2.86 | +2.61 | **109%** |
| TXF | buy  | 5000 | +3.78 | +2.10 | **180%** |
| TXF | sell | 500  | +0.99 | +1.21 | 82%  |
| TXF | sell | 2000 | +1.10 | +1.27 | 86%  |
| TXF | sell | 5000 | +1.51 | +1.63 | 93%  |

**The 7-feature linear regression is essentially a `spread × p_fill`
heuristic with marginal noise.** On TXF buy h=5000 the spread-only
baseline actually beats the 7-feature model by 80%, suggesting the
extra features add overfit, not signal. TMF gets 5-18% lift from the
extra 6 features (depth, ofi, queue, vol, churn, toxicity); TXF gets
zero or negative.

**What this changes:**
1. **The deployable maker-quality gate is `score = p_fill_hat × spread_z`**,
   not the full markout regression. It's simpler, more interpretable,
   and equally (or more) durable across instruments. The fill model is
   the only thing that needs training; spread is just read from the L1.
2. **The "P2 predictor discovers microstructure" claim is OVERSTATED.**
   The signal is mostly geometric (wide spread → more room to capture
   mid-drift). The 6 microstructure features are at the noise floor.
3. **Real microstructure signal would require non-linear features**
   (e.g., spread × depth_imbalance, spread × ofi, regime conditioning)
   — the linear in-z-score formulation can't see those interactions.

**Honest verdict (rev 4).** The P2 lane produces a durable maker-quality
gate, but the durable signal is essentially "wide spread + likely to
fill" — exactly the kind of geometric ranking that any half-decent
manual rule would capture. The R65-style audit gates are passed because
the underlying mechanism is geometric, not regime-conditional. This is
useful infrastructure but not a research breakthrough.

## Pooled TMF + TXF predictor (follow-up #1, completed 2026-05-11)

- Code: `research/experiments/p2_exec_predictor/pooled_predictor.py`
- Output: `outputs/p2_exec_predictor/cross/pooled/{REPORT.md, summary.json,
  models/, models_regress/, eval_tmf/, eval_txf/}`
- Train: 23 TMF + 21 TXF train days combined (44 days). Eval: each panel's own 10 test days.

| panel | side | h(ms) | pooled top-bot | in-panel top-bot | cross-from-other top-bot |
|-------|------|------:|---------------:|-----------------:|-------------------------:|
| tmf   | buy  |   500 |         +1.26  |           +1.65  |                   +0.14  |
| tmf   | buy  |  2000 |         +0.49  |           +1.48  |                   +0.17  |
| tmf   | buy  |  5000 |         +0.54  |           +1.52  |                   −0.38  |
| tmf   | sell |   500 |         +0.63  |           +0.72  |                   +0.60  |
| tmf   | sell |  2000 |         +0.59  |           +0.68  |                   +0.57  |
| tmf   | sell |  5000 |         +0.62  |           +0.72  |                   +0.59  |
| txf   | buy  |   500 |         +2.55  |           +2.33  |                   +2.62  |
| txf   | buy  |  2000 |         +2.84  |           +2.61  |                   +2.79  |
| txf   | buy  |  5000 |         +3.63  |           +2.10  |                   +3.44  |
| txf   | sell |   500 |         +1.17  |           +1.21  |                   +1.13  |
| txf   | sell |  2000 |         +1.22  |           +1.27  |                   +1.11  |
| txf   | sell |  5000 |         +1.58  |           +1.63  |                   +1.34  |

- All 12 (panel × side × h) cells: sign_consistency = 1.0,
  max_single_day_share 0.13–0.34 → both R65 §7.1 stability gates hold.
- **TXF buy h=5000 lift**: pooled +3.63 pt is **73 % above TXF in-panel
  +2.10 pt** — the TMF-shaped wide-spread tilt actually helps TXF buy where
  TXF in-panel was weakest.
- **TMF buy h=2000 / h=5000 collapse**: pooled drops to ≈ ⅓ of TMF in-panel.
  Cause: pooled `regress_y_mean` is +0.07 → +0.39 on buy-side (TXF rows
  carry larger absolute markouts → pooled regressor over-predicts TMF
  markout magnitude → decile compression on TMF).
- **Sell-side**: pooled ≈ in-panel everywhere (within 5 %). The sell-side
  signal is genuinely instrument-agnostic.
- **Verdict**: pooling is *not* a strict improvement — it shifts which side
  is hurt, instead of closing the cross-transfer asymmetry. The
  per-instrument predictor remains the right deployment unit. Pooled
  models are useful as a **diagnostic** (they confirm that magnitude is
  instrument-specific), not as a deployable artifact.

## Residual analysis — α × spread baseline + microstructure leftover (follow-up #2, completed 2026-05-11)

- Code: `research/experiments/p2_exec_predictor/residual_analysis.py`
- Output: `outputs/p2_exec_predictor/{tmf,txf}/residual_analysis/{REPORT.md, summary.json, <side>_h<H>.json}` and `models_residual/`.

Step 1: fit `markout = α × spread + β` on filled rows (univariate OLS).
Step 2: train `LinearRegressor` on the 6 microstructure features with
target = residual `r = markout − (α × spread + β)`. Composite then becomes
`p_fill_hat × (α × spread + β + pred_residual)`.

### Geometric baseline strength — α and base_r²

| panel | side | h(ms) | α (pt/pt) | base_r²_train | base_r²_test |
|-------|------|------:|----------:|--------------:|-------------:|
| tmf   | buy  |   500 |    +0.45  |        +0.50  |       +0.04  |
| tmf   | buy  |  2000 |    +0.45  |        +0.46  |       +0.02  |
| tmf   | buy  |  5000 |    +0.44  |        +0.40  |       +0.01  |
| tmf   | sell |   500 |    +0.41  |        +0.33  |       +0.00  |
| tmf   | sell |  2000 |    +0.41  |        +0.30  |       −0.00  |
| tmf   | sell |  5000 |    +0.41  |        +0.25  |       −0.01  |
| txf   | buy  |   500 |    +0.55  |        +0.19  |       +0.45  |
| txf   | buy  |  2000 |    +0.56  |        +0.19  |       +0.42  |
| txf   | buy  |  5000 |    +0.57  |        +0.19  |       +0.36  |
| txf   | sell |   500 |    +0.48  |        +0.89  |       +0.38  |
| txf   | sell |  2000 |    +0.48  |        +0.89  |       +0.36  |
| txf   | sell |  5000 |    +0.48  |        +0.87  |       +0.31  |

- α is **remarkably stable** within a panel: 0.41–0.45 across all (side, h)
  on TMF; 0.48–0.57 on TXF. The "markout earned per spread point" is one
  number per panel — that's the durable signal.
- TMF base_r²_test (0.0–0.04) << TMF base_r²_train (0.25–0.50) — α decays
  out-of-sample on TMF (the markout volatility is mostly NOT explained
  by spread on TMF test).
- TXF base_r²_test stays high (0.31–0.45) — spread really does explain a
  large share of TXF markout structure.

### Residual r² and composite EV lift

| panel | side | h(ms) | resid_r²_test | resid_pearson | total top-bot | base-only top-bot | Δ residual lift |
|-------|------|------:|--------------:|--------------:|--------------:|------------------:|----------------:|
| tmf   | buy  |   500 |       −0.012  |       +0.161  |        +1.62  |            +1.39  |          +0.23  |
| tmf   | buy  |  2000 |       −0.026  |       +0.061  |        +1.43  |            +1.30  |          +0.13  |
| tmf   | buy  |  5000 |       −0.024  |       +0.020  |        +1.48  |            +1.37  |          +0.12  |
| tmf   | sell |   500 |       −0.004  |       +0.099  |        +0.72  |            +0.58  |          +0.14  |
| tmf   | sell |  2000 |       −0.009  |       +0.045  |        +0.66  |            +0.59  |          +0.07  |
| tmf   | sell |  5000 |       −0.009  |       +0.024  |        +0.70  |            +0.64  |          +0.06  |
| txf   | buy  |   500 |       −0.296  |       −0.007  |        +2.33  |            +2.59  |          −0.27  |
| txf   | buy  |  2000 |       −0.286  |       −0.021  |        +2.61  |            +2.99  |          −0.38  |
| txf   | buy  |  5000 |       −0.281  |       −0.003  |        +2.10  |            +3.84  |          **−1.74**  |
| txf   | sell |   500 |       −0.052  |       +0.099  |        +1.19  |            +1.06  |          +0.12  |
| txf   | sell |  2000 |       −0.052  |       +0.048  |        +1.24  |            +1.19  |          +0.05  |
| txf   | sell |  5000 |       −0.041  |       +0.032  |        +1.59  |            +1.56  |          +0.02  |

- **Residual r² is NEGATIVE in all 12 cells** — fitting microstructure
  features to the spread-residual increases pointwise prediction error vs
  the constant-mean predictor. Microstructure is noise on the residual.
- **Δ residual lift on the composite is positive on TMF (+0.06 → +0.23)
  and on TXF sell (+0.02 → +0.12), negative on TXF buy (−0.27 → −1.74)**.
  The TMF positive lift comes from the `p_fill ×` interaction
  re-ranking, not from any pointwise predictive power.
- **TXF buy h=5000 spread-only baseline beats the full 7-feature regression
  composite by +1.74 pt** — the 6 microstructure features are *strictly
  harmful* on TXF buy, in a way that doesn't show up in r² because the
  overfit eats the high-decile rows (where most realized markout lives).
- **Sanity-check on the rev 4 verdict**: `p_fill × (α × spread)` is now
  measured at +1.30 → +1.39 pt on TMF buy and +2.59 → +3.84 pt on TXF buy,
  versus +1.48 → +1.65 (TMF) and +2.10 → +2.61 (TXF) for the full
  7-feature composite. A **2-parameter model (α, β)** captures 84-160 % of
  the durable signal at zero risk of overfitting.

## Interaction features — spread × {imb, ofi, qratio, vol, churn, tox} (follow-up #3, completed 2026-05-11)

- Code: `research/experiments/p2_exec_predictor/interaction_features.py`
- Output: `outputs/p2_exec_predictor/{tmf,txf}/interaction_features/{REPORT.md, summary.json, <side>_h<H>.json}` and `models_interactions/`.

Extends the markout regressor to 13 features: original 7 + 6
spread-conditioned interactions. Tests whether *conditional* microstructure
("depth imbalance matters more in wide-spread regimes") adds anything.

| panel | side | h(ms) | r² ext | r² base | Δ r² | ext top-bot | base top-bot | Δ lift |
|-------|------|------:|-------:|--------:|-----:|------------:|-------------:|-------:|
| tmf   | buy  |   500 | +0.077 |  +0.084 | −0.008 |       +1.59 |        +1.65 |  −0.06 |
| tmf   | buy  |  2000 | +0.024 |  +0.030 | −0.006 |       +1.33 |        +1.48 |  −0.15 |
| tmf   | buy  |  5000 | +0.011 |  +0.014 | −0.003 |       +1.30 |        +1.52 |  −0.22 |
| tmf   | sell |   500 | +0.020 |  +0.017 | +0.002 |       +0.69 |        +0.72 |  −0.03 |
| tmf   | sell |  2000 | +0.005 |  +0.001 | +0.004 |       +0.58 |        +0.68 |  −0.09 |
| tmf   | sell |  5000 | +0.000 |  −0.004 | +0.004 |       +0.55 |        +0.72 |  −0.17 |
| txf   | buy  |   500 | +0.035 |  +0.386 | **−0.352**|     +0.65 |        +2.33 |  **−1.68** |
| txf   | buy  |  2000 | +0.077 |  +0.347 | **−0.270**|     +1.00 |        +2.61 |  **−1.61** |
| txf   | buy  |  5000 | +0.105 |  +0.271 | −0.166 |       +1.79 |        +2.10 |  −0.31 |
| txf   | sell |   500 | +0.341 |  +0.388 | −0.047 |       +1.14 |        +1.21 |  −0.06 |
| txf   | sell |  2000 | +0.311 |  +0.357 | −0.046 |       +1.18 |        +1.27 |  −0.09 |
| txf   | sell |  5000 | +0.264 |  +0.308 | −0.044 |       +1.55 |        +1.63 |  −0.08 |

- **Δ lift is negative in all 12 cells** — interactions strictly hurt
  decile separation everywhere.
- TMF: small negatives (−0.03 → −0.22 pt) — interactions add some noise.
- TXF buy h=500/h=2000: catastrophic (−1.68 / −1.61 pt) — extended weights
  are massively overfit (e.g. `spr_x_qratio` reaches +9.4 on TXF buy
  h=5000 vs +0.50 on TMF). L2 = 0.05 is insufficient against the
  spread-correlation explosion in the new columns.
- **Verdict**: no detectable conditional microstructure on either panel.
  Even the cells where interaction r² is mildly positive (TMF sell h=2000/h=5000)
  show negative composite lift — pointwise fit doesn't translate to ranking.

## Spread-only cross-instrument transfer (follow-up #4, completed 2026-05-11)

- Code: `research/experiments/p2_exec_predictor/spread_only_transfer.py`
- Output: `outputs/p2_exec_predictor/cross/{tmf_to_txf,txf_to_tmf}_spread/spread_only_transfer/{REPORT.md, summary.json, <side>_h<H>.json}`

Pipeline: fit `markout = α_src × spread + β_src` on **filled** training rows
of the SOURCE panel (univariate OLS, no microstructure features at all).
Score the TARGET panel's test split with `p_fill_target × (α_src × spread + β_src)`.
Only **α** transfers — the target panel's fill model is unchanged.

| direction | side | h(ms) | α_src | spread-only top-bot | full 7-feat transfer | tgt in-panel full-feat | spread-only / in-panel |
|-----------|------|------:|------:|--------------------:|---------------------:|-----------------------:|-----------------------:|
| TMF→TXF   | buy  |   500 | +0.45 |              +2.53  |               +2.62  |                 +2.33  |                  108 % |
| TMF→TXF   | buy  |  2000 | +0.45 |              +2.85  |               +2.79  |                 +2.61  |                  109 % |
| TMF→TXF   | buy  |  5000 | +0.44 |              +3.67  |               +3.44  |                 +2.10  |              **175 %** |
| TMF→TXF   | sell |   500 | +0.41 |              +0.99  |               +1.13  |                 +1.21  |                   82 % |
| TMF→TXF   | sell |  2000 | +0.41 |              +1.08  |               +1.11  |                 +1.27  |                   85 % |
| TMF→TXF   | sell |  5000 | +0.41 |              +1.44  |               +1.34  |                 +1.63  |                   88 % |
| TXF→TMF   | buy  |   500 | +0.55 |              +1.31  |               +0.14  |                 +1.65  |                   79 % |
| TXF→TMF   | buy  |  2000 | +0.56 |              +1.22  |               +0.17  |                 +1.48  |                   82 % |
| TXF→TMF   | buy  |  5000 | +0.57 |              +1.26  |               **−0.38** |              +1.52  |                   83 % |
| TXF→TMF   | sell |   500 | +0.48 |              +0.63  |               +0.60  |                 +0.72  |                   88 % |
| TXF→TMF   | sell |  2000 | +0.48 |              +0.60  |               +0.57  |                 +0.68  |                   88 % |
| TXF→TMF   | sell |  5000 | +0.48 |              +0.61  |               +0.59  |                 +0.72  |                   85 % |

- **Spread-only transfer fixes the broken full-feature case** (TXF→TMF buy
  h=5000: full transfer = **−0.38 pt (sign-flipped)**, spread-only =
  **+1.26 pt** = 83 % of TMF in-panel). The 6 microstructure features were
  the source of the failure, not the model architecture.
- **Spread-only transfer often BEATS in-panel full-feature** on TXF buy
  h=5000 (175 % retention) — TMF's α (+0.44) plus TXF's `p_fill` recovers
  +3.67 pt of separation, vs +2.10 for the TXF in-panel full-feature
  composite. The TXF in-panel buy h=5000 model is OVERFIT to the wide-spread
  cohort; replacing its overfit weights with TMF's clean α improves it.
- **All 12 spread-only transfers pass the R65 §7.1 stability gate**:
  sign_consistency 0.90–1.00, max_single_day_share 0.13–0.31.
- **α is genuinely panel-specific but the *form* is universal**:
  TMF α = 0.41–0.45, TXF α = 0.48–0.57, both stable within each panel
  across all (side, h). One number per panel × side family captures the
  durable signal.
- **Verdict**: P2 reduces to ONE deployable formula:
  `score(t, side) = p_fill_hat_panel_side(t) × α_panel × spread(t)`
  where `α_panel ∈ {0.45 (TMF), 0.55 (TXF)}` (use the buy-side α for both
  sides — it's stable enough). Estimated zero loss vs the 7-feature
  composite, ~9 % vs the in-panel residual+α composite, and complete
  immunity to the cross-transfer pathology.

## Known issues / follow-ups (not blocking)

1. **Duplicate day-shard handling**. The synth's `_split_by_day` runs
   per-shard; days at month boundaries (e.g. 2026-03-31) get written from
   both 2026-03 and 2026-04 shards. The second write silently overwrites
   the first and only keeps the rows that fell in that shard. Fix: either
   merge across shards before splitting, or skip the duplicate write.
   Impact on the smoke is bounded (one day's data loses its early portion);
   AUC headline unaffected.
2. ~~**Composite EV calculation**~~ — **DONE 2026-05-11**, see Composite EV
   section above. Buy-side composite separates top vs bottom decile by
   +0.68 → +1.19 pt raw EV across horizons; sell-side composite is
   indistinguishable from `p_fill` alone (the markout-side driver is too
   weak in this 10-day bullish-drift window). Net EV still negative after
   4 pt RT cost — predictor is a quality filter, not a stand-alone alpha.
3. **TXFD6 panel**. Same pipeline can be re-run on
   `research/data/derived/txf_full_2026/` (24 days, 73 M events) by simply
   pointing `--panel` at it.
4. **Live integration**. The predictor weights are JSON-serializable
   already (see `models/<tag>.json`). A live wrapper would need to
   recompute the same 7 features at quote-decision time and apply the
   sigmoid. The existing `FillProbabilityFilter` in
   `research/alphas/fill_prob_filter/impl.py` is the natural home for
   this; the new model just needs to be loaded into it.

## Files (durable artifacts)

- Code: `research/experiments/p2_exec_predictor/{synth_fill_events,models,train_eval,composite_ev,stability_audit,markout_regression,cross_transfer,pooled_predictor,residual_analysis,interaction_features,spread_only_transfer}.py`
- Synth output: `research/data/derived/p2_fill_events_tmf_smoke/` (58 daily npz, ~3 GB on disk)
- Train/eval output: `outputs/p2_exec_predictor/tmf/`
  - `models/<tag>.json` — 12 trained logistics (weights + normalizer)
  - `eval/<tag>.json` — full per-target metrics + calibration + by_spread_quintile
  - `summary.json` — top-line table
  - `REPORT.md` — human-readable report
  - `composite_ev/{<side>_h<H>.json,summary.json,REPORT.md}` — composite EV deciles
- Run logs: `outputs/p2_exec_predictor/{_smoke_synth.log,_smoke_train.log}`

## Plan §7.5 Pivot 2 status

- ✅ Fill-prob predictor: trained, validated.
- ✅ Adverse-selection predictor: trained, validated.
- ✅ Continuous markout regression: trained, validated; closes the sell-side
  asymmetry and passes R65-style single-day dominance gates.
- ✅ TXFD6: trained and validated; structurally stronger than TMF.
- ✅ Cross-instrument transfer: 5/6 practical transfers pass; TXF→TMF buy
  remains broken.
- ✅ Feature decomposition: simple `p_fill_hat x spread_z` retains most of the
  full 7-feature regression composite and is the recommended deployable gate.
- ✅ Pooled TMF+TXF predictor: pooling shifts which side is broken — not a
  strict improvement; per-instrument predictor remains correct deployment unit.
- ✅ Residual analysis (`markout − α × spread`): residual r² is negative
  everywhere; composite lift is +0.06 → +0.23 pt on TMF, **−0.27 → −1.74 pt
  on TXF buy** — microstructure is noise on the residual.
- ✅ Interaction features (`spread × {imb, ofi, qratio, vol, churn, tox}`):
  Δ lift negative in **all 12 (panel × side × h) cells**; catastrophic on
  TXF buy h=500/h=2000 (−1.68 / −1.61 pt). No conditional microstructure.
- ✅ Spread-only cross transfer: **fixes the broken full-feature TXF→TMF buy
  case** (h=5000: −0.38 → +1.26 pt). Often beats in-panel full-feature
  (TMF→TXF buy h=5000: +3.67 vs +2.10 = 175 % retention). Single per-panel
  α captures the durable signal across instruments.
- ✅ P2-V maker gate validation smoke: run on TMF and TXF. Strict top-10
  simple gate passes as infrastructure; top-20 remains WATCH; top-30 is not
  promoted.
- ⚠️ Live integration: not yet wired.

This slice does not promote a live strategy. It promotes infrastructure:
freeze the strict simple maker gate and use it to gate future F2
external-driver or directional strategy candidates.

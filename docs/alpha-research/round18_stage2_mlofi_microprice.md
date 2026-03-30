# Round 18 — Stage 2: MLOFI Microprice Correction (Candidate A)

**Date**: 2026-03-27
**Alpha ID**: `mlofi_microprice_correction`
**Status**: CONDITIONAL PASS (asset-specific — see recommendation)

## Summary

MLOFI-driven microprice correction tested on TXFD6 (primary) and 2330/TSMC (secondary) using L5 order book data. The signal computes geometrically-weighted multi-level OFI (levels 1-5) with EMA-8 smoothing, then applies a regression-derived coefficient to produce a microprice correction term.

**Key finding**: MLOFI has ZERO predictive power on TXFD6 but STRONG predictive power on 2330. The signal is asset-class dependent, not universally applicable.

## Data

| Asset | Rows | Days | L5 Coverage | Source |
|-------|------|------|-------------|--------|
| TXFD6 (TXFE6) | 3,405,375 | 15 (Mar 3-23) | Variable (0-100%, mean ~55%) | `research/data/l5_v2/TXFE6_l5.npy` |
| 2330 (TSMC) | 1,009,983 | 17 (Mar 2-24) | 100% all days | `research/data/l5_v2/2330_l5.npy` |

Note: TXFD6 L5 coverage is inconsistent — several days have <40% L5 data (Mar 6: 5.6%, Mar 11: 0.7%, Mar 13: 0%), which degrades signal quality. 2330 has full L5 on all days.

## IC Decay Curve — TXFD6 (Primary Kill Gate Asset)

| Horizon | Mean IC | Std IC | NW t-stat | N days |
|---------|---------|--------|-----------|--------|
| 250ms | +0.0006 | 0.0101 | 0.23 | 14 |
| 500ms | +0.0003 | 0.0095 | 0.13 | 14 |
| 1s | +0.0012 | 0.0114 | 0.34 | 14 |
| 2s | -0.0000 | 0.0134 | -0.00 | 14 |
| 5s | -0.0014 | 0.0214 | -0.23 | 14 |
| 10s | -0.0060 | 0.0284 | -0.75 | 14 |
| **30s** | **-0.0198** | **0.0462** | **-1.71** | **14** |
| 60s | -0.0392 | 0.0600 | -3.08 | 14 |

**Kill gate**: |IC(30s)| = 0.0198 >= 0.015 threshold. **Technically PASSES** by absolute value.

However, the IC is **NEGATIVE** at 30s and 60s (NW t = -1.71 and -3.08), meaning MLOFI is **anti-predictive** at longer horizons on TXFD6. At short horizons (250ms-2s), IC is indistinguishable from zero (|t| < 0.35). This is not a viable signal.

## IC Decay Curve — 2330 (Secondary)

| Horizon | Mean IC | Std IC | NW t-stat | N days |
|---------|---------|--------|-----------|--------|
| 250ms | +0.0934 | 0.0290 | 17.45 | 17 |
| 500ms | +0.0928 | 0.0248 | 21.33 | 17 |
| 1s | +0.1011 | 0.0262 | 23.22 | 17 |
| 2s | +0.1183 | 0.0265 | 28.03 | 17 |
| 5s | +0.1429 | 0.0298 | 26.27 | 17 |
| 10s | +0.1715 | 0.0271 | 38.46 | 17 |
| **30s** | **+0.2060** | **0.0320** | **39.97** | **17** |
| 60s | +0.2118 | 0.0381 | 33.47 | 17 |

Kill gate: |IC(30s)| = 0.206 >> 0.015. **STRONG PASS**.

IC is monotonically increasing from +0.093 at 250ms to +0.212 at 60s. All 17 days show positive IC. NW t-stats range from 17 to 40 — extremely statistically significant.

## Regression Coefficients

### TXFD6
- Mean alpha: +223,631 (wildly unstable)
- CV(alpha): **337.1%** — FAILS stability gate (>50%)
- Mean R-squared: 0.0012 (essentially zero)
- L1-only R-squared: 0.0005
- Improvement: +135% (but 0.0012 vs 0.0005 is both meaningless)

### 2330
- Mean alpha: +17.16 (price_x10000 units per MLOFI unit)
- CV(alpha): **56.1%** — borderline FAIL (>50%)
- Mean R-squared: 0.00166
- L1-only R-squared: 0.00085
- Improvement: **+94.7%** (meaningful: MLOFI nearly doubles R-squared vs L1-only)

Note: Alpha is POSITIVE on both assets (13/14 days for TXFD6, 16/17 for 2330). This contradicts the initial hypothesis that MLOFI should be contrarian (negative alpha) on TWSE. The data shows MLOFI is **pro-cyclical**: positive MLOFI (bid refill > ask refill) predicts price UP.

## Incremental IC Analysis (MLOFI Correction vs L1 Residual)

### TXFD6
| Horizon | Incremental IC | NW t-stat |
|---------|---------------|-----------|
| 250ms | -0.1695 | -3.98 |
| 500ms | -0.1718 | -4.08 |
| 1s | -0.1712 | -4.13 |
| 30s | -0.1513 | -4.14 |
| 60s | -0.1467 | -4.03 |

**Deeply negative**: The MLOFI correction term is actively WRONG on TXFD6. Adding it makes predictions worse than L1 alone.

### 2330
| Horizon | Incremental IC | NW t-stat |
|---------|---------------|-----------|
| 250ms | -0.0229 | -3.32 |
| 500ms | -0.0163 | -2.45 |
| 1s | -0.0077 | -1.21 |
| 2s | +0.0058 | +0.88 |
| 5s | +0.0305 | +3.50 |
| 10s | +0.0605 | +5.09 |
| **30s** | **+0.1201** | **+6.54** |
| 60s | +0.1479 | +6.94 |

**Positive at horizons >= 5s**: The correction term adds genuine new information beyond L1 for 2330 at medium-to-long horizons. IC = +0.12 at 30s with NW t = 6.54.

Crossover point: ~2s. Below 2s, L2-L5 information hurts (noise > signal). Above 2s, it helps.

## Lambda Sensitivity

### TXFD6 (at 5s horizon)
| Lambda | Mean IC | NW t |
|--------|---------|------|
| 0.30 | -0.0010 | -0.16 |
| 0.40 | -0.0011 | -0.17 |
| 0.50 | -0.0014 | -0.23 |
| 0.60 | -0.0011 | -0.17 |
| 0.70 | -0.0007 | -0.11 |

No lambda value produces meaningful IC. The signal is fundamentally absent on TXFD6.

### 2330 (at 5s horizon)
| Lambda | Mean IC | NW t |
|--------|---------|------|
| 0.30 | +0.1456 | 26.53 |
| 0.40 | +0.1444 | 26.47 |
| 0.50 | +0.1429 | 26.27 |
| 0.60 | +0.1413 | 26.15 |
| 0.70 | +0.1394 | 26.21 |

Lambda = 0.3 is marginally best but all values work. Steeper L1 weighting preferred.

## Realized Spread Per Fill

Simulated fills when |MLOFI| > 0.5 threshold, measured as mid(t) vs mid(t+30s):

### TXFD6
- Very few fills (15-83 per day) — signal too weak for meaningful fill generation
- Mean realized spread: negative on most days (adverse selection dominates)

### 2330
- High fill rate: 16K-55K fills/day
- Mean realized spread: consistently negative (-2K to -6K in x10000 units)
- **Even with strong IC, realized spread is negative** — the signal predicts direction but the prediction doesn't overcome the bid-ask spread cost

## TWSE Sign Finding

**Original hypothesis**: MLOFI gradient is CONTRARIAN on TWSE (positive MLOFI = passive refill = price DOWN, so alpha < 0).

**Empirical result**: Alpha is POSITIVE on 29/31 asset-days. MLOFI is **pro-cyclical** on TWSE: when bids are refilled more than asks across L1-L5, price goes UP. This is consistent with:
- Bid refill = demand strength, not passive market-making
- The "contrarian" interpretation from Round 11 (MLDM) was based on different signal construction (L2-L5 only, without L1)
- Including L1 in the OFI integration flips the sign because L1 dominates

## Kill Gate Results

| Gate | Asset | Metric | Value | Threshold | Result |
|------|-------|--------|-------|-----------|--------|
| IC(30s) | TXFD6 | \|IC\| | 0.0198 | >= 0.015 | **MARGINAL PASS** |
| IC(30s) | 2330 | IC | +0.206 | >= 0.015 | **STRONG PASS** |
| CV(alpha) | TXFD6 | CV% | 337% | <= 50% | **FAIL** |
| CV(alpha) | 2330 | CV% | 56% | <= 50% | **BORDERLINE FAIL** |
| R2 improvement | TXFD6 | delta | +0.07% | >= 5% | **PASS** (but meaningless magnitude) |
| R2 improvement | 2330 | delta | +95% | >= 5% | **PASS** |
| Incremental IC(30s) | TXFD6 | IC | -0.151 | > 0 | **FAIL** |
| Incremental IC(30s) | 2330 | IC | +0.120 | > 0 | **PASS** |

## Prototype Code

- Alpha implementation: `research/alphas/mlofi_microprice/impl.py`
- Backtest/IC analysis: `research/alphas/mlofi_microprice/backtest_ic.py`
- Unit tests (12 tests, all passing): `research/alphas/mlofi_microprice/tests/test_logic.py`

## Recommendation

### TXFD6: TERMINATE

MLOFI has no predictive power on TXFD6 futures. IC is indistinguishable from zero at short horizons and actively negative at longer horizons. Coefficient is wildly unstable (CV=337%). Incremental IC is deeply negative (-0.15). There is no parameterization or weighting scheme that rescues this signal.

**Root cause**: TXFD6 is a single front-month contract with thin depth (L5 coverage only 55% of ticks). The order book dynamics are dominated by a few large participants, and depth changes at L2-L5 are mostly noise (price level shifts, contract rolls) rather than information.

### 2330 (TSMC Equity): CONDITIONAL PASS for Stage 3

MLOFI shows strong, consistent predictive power on 2330:
- IC = +0.093 to +0.212 across all horizons (17/17 days positive)
- Incremental IC = +0.12 at 30s beyond L1 (NW t = 6.54)
- R-squared nearly doubles vs L1-only

**However, critical blockers remain**:

1. **Realized spread is negative**: Even with strong IC, the directional prediction does not overcome bid-ask costs. This means the signal is NOT directly monetizable as a standalone market-making signal.

2. **Coefficient instability**: CV = 56% (just above 50% threshold). Needs regularization or cross-validated fitting.

3. **Integration path unclear**: OpMM strategy is designed for TXFD6, not equities. 2330 would need a separate strategy or cross-asset signal pathway.

4. **No live latency profile**: The signal is measured on L5 snapshots — no fill simulation with realistic queue priority or latency.

### Recommended next steps (if proceeding on 2330)

1. Test as a CBS filter for TSMC strategies (similar to 2330-as-TXFD6-filter from R17)
2. Evaluate as FeatureEngine v2 candidate (feature-only, not standalone alpha)
3. Regularize alpha coefficient (rolling ridge regression, not OLS)
4. Cross-validate with IS/OOS split (first 10 days IS, last 7 days OOS)

### For TXFD6 OpMM integration: NOT RECOMMENDED

The original scope (OpMM `on_features()` integration for TXFD6 microprice) is **dead**. MLOFI adds no value on TXFD6.

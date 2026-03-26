# Stage 2: Prototype Results — ISS + MLDM

**Date**: 2026-03-25
**Data**: TXFD6 L5, 2,171,578 rows (10 trading days, Feb-Mar 2026)
**Symbols**: TXFD6 (TAIFEX mini-TAIEX futures)

---

## 1. Implementations

### Impact Surprise Signal (ISS)
- **File**: `research/alphas/impact_surprise/impl.py`
- **Class**: `ImpactSurpriseAlpha`
- **Baseline modes**: `"depth"` (Cont 2014: 1/(2D)) and `"ema"` (slow EMA of b_hat, span=2000)
- **Session masking**: configurable `session_mask_ticks` (default 300 = ~11s at 27 ticks/s)
- **Dead-zone**: |deviation| < 0.3 -> signal = 0 (Challenger B-1)
- **Bayesian shrinkage**: when var(OFI) < 0.01, b_hat shrinks to b_eq
- **Tests**: 21 passed

### Multi-Level Depth Momentum (MLDM)
- **File**: `research/alphas/mldm_depth_momentum/impl.py`
- **Class**: `MultiLevelDepthMomentumAlpha`
- **BBO-shift guard**: when best bid or ask price changes, deep_net zeroed (Execution review)
- **Thin LOB guard**: need >= 2 levels for L2+ signal
- **L1 exclusion**: only uses index 1-4 (L2-L5), L1 excluded by construction
- **Tests**: 26 passed (including `test_bbo_shift_zeros_signal`, `test_l1_only_change_no_signal`)

---

## 2. IC Measurements (Spearman rank correlation with forward mid-price return)

| Signal | h=10 ticks | h=50 ticks | h=200 ticks | h=500 ticks | Notes |
|--------|-----------|-----------|------------|------------|-------|
| ofi_raw (baseline) | -0.006 | +0.006 | +0.001 | -0.001 | Essentially zero IC |
| **ISS (depth baseline)** | -0.001 | -0.002 | -0.015 | -0.017 | Negative at long horizons -- WRONG SIGN |
| **ISS (ema baseline)** | **+0.009** | **+0.014** | **+0.016** | +0.008 | Best at h=50-200 (65% active) |
| **MLDM** | **+0.015** | **+0.015** | +0.009 | +0.004 | Best at h=10-50 |
| ofi_depth_divergence | +0.028 | +0.017 | +0.010 | +0.004 | Existing alpha (reference) |

### Key Observations

1. **ISS (ema) outperforms ISS (depth)** at every horizon. The self-referencing EMA baseline (Challenger B-2) is the correct choice. ISS (depth) produces wrong-sign IC at long horizons -- likely because 1/(2D) is a poor equilibrium estimate for TXFD6 futures where depth is highly variable.

2. **MLDM shows IC = +0.015 at h=10-50** -- meaningful short-horizon predictive power from deep-book dynamics alone. This is independent of L1 features.

3. **Both signals exceed the Challenger kill criterion** of IC > 0.01 marginal over OFI (which has near-zero IC).

4. **ISS (ema) at h=200 (+0.016)** is the strongest new signal at medium horizons, complementing MLDM's short-horizon strength.

---

## 3. Collinearity Matrix (Pearson correlation, post-warmup)

|  | ofi_raw | ISS_depth | ISS_ema | MLDM | OFI_depth_div |
|--|---------|-----------|---------|------|---------------|
| ofi_raw | 1.000 | -0.000 | -0.001 | 0.006 | 0.018 |
| ISS_depth | -0.000 | 1.000 | 0.662 | 0.000 | 0.000 |
| ISS_ema | -0.001 | 0.662 | 1.000 | -0.000 | 0.000 |
| MLDM | 0.006 | 0.000 | -0.000 | 1.000 | -0.048 |
| OFI_depth_div | 0.018 | 0.000 | 0.000 | -0.048 | 1.000 |

### Key Findings

- **ISS vs OFI: r = 0.000** -- completely uncorrelated as designed (second-order signal)
- **MLDM vs OFI: r = 0.006** -- near-zero collinearity confirming L1 exclusion works
- **MLDM vs OFI_depth_divergence: r = -0.048** -- small negative correlation (expected: MLDM is absolute momentum while ODD is shallow-vs-deep divergence)
- **ISS depth vs ISS ema: r = 0.662** -- moderately correlated (both measure impact deviation but with different baselines)

---

## 4. Signal Statistics (post-warmup)

| Signal | Mean | Std | % Nonzero | Range |
|--------|------|-----|-----------|-------|
| ofi_raw | -0.000 | 0.677 | 9.4% | [-36, 60] |
| ISS (depth) | +0.532 | 0.500 | 53.3% | [-1, 1] |
| ISS (ema) | -0.347 | 0.602 | 65.1% | [-1, 1] |
| MLDM | -0.000 | 0.025 | 100% | [-0.77, 1.19] |
| OFI_depth_div | +0.000 | 0.041 | 100% | [-2, 2] |

- ISS (depth) is biased positive (mean=0.53) -- b_hat consistently exceeds 1/(2D), confirming the depth baseline is miscalibrated for TXFD6.
- ISS (ema) is moderately biased negative (mean=-0.35) -- suggests b_hat generally falls below the slow EMA, reasonable for mean-reverting impact.
- MLDM is well-centered (mean~0) -- no regime bias.

---

## 5. Recommendations for Stage 3-4

1. **ISS: Use EMA baseline mode only.** Depth baseline is wrong-signed. Drop `baseline_mode="depth"` from further testing.

2. **MLDM: Proceed as-is.** IC > 0.01 at h=10-50, orthogonal to L1 features, BBO-shift guard working.

3. **Combination potential**: ISS (ema) and MLDM have r = 0.000 correlation and complementary horizon profiles (MLDM: short h=10-50, ISS: medium h=50-200). A combined signal may capture both regimes.

4. **Next**: Run on 2330 L5 data, compute per-day IC stability, estimate Sharpe under realistic latency assumptions.

# Round 18 Stage 2b: MLOFI Microprice Correction — Bug-Fixed Reanalysis

**Date**: 2026-03-27
**Alpha**: `mlofi_microprice_correction`
**Symbol**: 2330 (TSMC equity), TXFD6 (Mini-TAIEX futures)
**Data**: 17 days (2330), 15 days (TXFD6), March 2-24 2026
**Stage 2a reference**: `round18_stage2a_mlofi_microprice.md` (if exists)

## Bugs Fixed

| Bug | Location | Problem | Fix |
|-----|----------|---------|-----|
| **B1** | `impl.py:147-150` | `np.zeros()` allocation on every `update()` call | Pre-allocated `_cur_bid_price`/`_cur_ask_price` in `__init__`, use `[:] = 0` |
| **B2** | `backtest_ic.py:183-188` | `valid = future_idx < n` always True after `np.clip` | Compute `valid` from `future_idx_raw` BEFORE clipping |
| **B3** | `backtest_ic.py:557-562` | Realized spread assumed contrarian (negative direction) | Changed to pro-cyclical: `direction * (mid_future - mid_at_fill)` |

**B2 impact**: Minimal in practice. The `too_far` filter on L191-194 already catches most OOB cases. IC numbers unchanged.

**B3 impact**: Realized spread sign flipped from negative to positive, confirming pro-cyclical direction is correct.

## Challenge 1: Non-Overlapping IC (MUST)

Subsampled every horizon interval (30s -> ~550 obs/day, 60s -> ~277 obs/day).

### 2330 Non-Overlapping IC at 30s and 60s

| Date | IC(30s) | n | IC(60s) | n |
|------|---------|---|---------|---|
| 2026-03-24 | +0.159 | 549 | +0.095 | 277 |
| 2026-03-23 | +0.085 | 248 | +0.163 | 124 |
| 2026-03-20 | +0.109 | 550 | +0.132 | 277 |
| 2026-03-19 | +0.112 | 550 | +0.122 | 277 |
| 2026-03-18 | +0.127 | 546 | +0.138 | 276 |
| 2026-03-17 | +0.149 | 547 | +0.109 | 276 |
| 2026-03-16 | +0.154 | 550 | +0.090 | 277 |
| 2026-03-13 | +0.174 | 497 | +0.198 | 250 |
| 2026-03-12 | +0.072 | 552 | +0.202 | 277 |
| 2026-03-11 | +0.118 | 551 | +0.097 | 277 |
| 2026-03-10 | +0.181 | 552 | +0.202 | 277 |
| 2026-03-09 | +0.129 | 555 | +0.209 | 278 |
| 2026-03-06 | +0.147 | 550 | +0.170 | 277 |
| 2026-03-05 | +0.189 | 552 | +0.158 | 278 |
| 2026-03-04 | +0.150 | 548 | +0.224 | 277 |
| 2026-03-03 | +0.074 | 558 | +0.147 | 280 |
| 2026-03-02 | +0.150 | 400 | +0.144 | 201 |

### Pooled Non-Overlapping IC

| Horizon | Overlapping IC | Non-Overlapping IC | Drop | NW t (non-overlap) |
|---------|---------------|--------------------|------|--------------------|
| 30s | +0.206 | **+0.134** | -35% | **19.51** |
| 60s | +0.212 | **+0.153** | -28% | **15.19** |

**Assessment**: IC drops ~30-35% when eliminating overlap inflation, as expected. However, non-overlapping IC remains strongly positive at +0.134 (30s) with NW t=19.51, far above the kill gate. All 17 days are positive. The overlapping IC was inflated but the signal is NOT an artifact.

## Challenge 2: Detrended IC (MUST)

Removed local 5-minute rolling mean from forward returns before computing Spearman IC. This changes rank order (unlike constant subtraction).

### 2330 Detrended IC (5-min local trend removed)

| Horizon | Raw IC | Detrended IC | NW t (detrend) |
|---------|--------|-------------|----------------|
| 250ms | +0.093 | **-0.215** | -19.45 |
| 500ms | +0.093 | **-0.214** | -19.43 |
| 1s | +0.101 | **-0.201** | -18.40 |
| 2s | +0.118 | **-0.181** | -15.78 |
| 5s | +0.143 | **-0.151** | -15.51 |
| 10s | +0.172 | **-0.114** | -14.80 |
| 30s | +0.206 | **-0.032** | -3.95 |
| 60s | +0.212 | **+0.025** | +2.21 |

**CRITICAL FINDING**: After removing local trends, IC flips NEGATIVE at all horizons below 60s. At 30s the detrended IC is -0.032 (below the kill gate of 0.015). Only at 60s does a faint positive residual (+0.025, t=2.21) survive.

**Interpretation**: The MLOFI EMA is a slow-moving signal that tracks the 5-minute local trend direction. When mid price is trending up, MLOFI is positive (pro-cyclical); the 30s forward return is also positive. The correlation is not microstructure alpha -- it is redundant trend-following. Within each 5-minute window, MLOFI actually predicts NEGATIVE returns (mean reversion against the signal), explaining the strongly negative short-horizon detrended IC.

## Challenge 3: L1-only vs L2-L5-only IC Decomposition (SHOULD)

### 2330 IC Decomposition

| Horizon | Full MLOFI IC | L1-only IC | L2-L5-only IC | L1 NW t | Deep NW t |
|---------|--------------|-----------|--------------|---------|-----------|
| 5s | +0.143 | **+0.148** | +0.027 | 24.68 | 5.91 |
| 30s | +0.206 | **+0.217** | +0.029 | 37.00 | 7.53 |

**Finding**: L1 alone produces IC=+0.217 at 30s, which is actually HIGHER than the full MLOFI (IC=+0.206). The L2-L5 deep levels contribute only IC=+0.029 (marginal). The "multi-level value-add" claimed in Stage 2a (+0.12 incremental IC) was almost entirely L1 imbalance, not deep book information.

Note: `compute_mlofi_l1_only` uses `lam=0.0`, which means weights=[1, 0, 0, 0, 0] -- pure L1 delta OFI with EMA.

## Challenge 4: OOS Incremental IC (SHOULD)

Rolling day-behind alpha: for day t, use day t-1's regression coefficient.

### 2330 OOS Incremental IC

| Horizon | In-Sample Incr IC | OOS Incr IC | OOS NW t |
|---------|------------------|------------|----------|
| 5s | +0.031 | **+0.033** | 3.98 |
| 30s | +0.120 | **+0.119** | 6.32 |

Per-day OOS incremental IC at 30s:
- 14/16 days positive (88%)
- Two negative days: 2026-03-23 (-0.103), 2026-03-16 (weakly at +0.060 but still positive)
- Mean: +0.119, consistent with in-sample

**Assessment**: OOS incremental IC is stable and nearly identical to in-sample. The regression alpha is sufficiently stable day-to-day for the correction term to add value even with a lagged coefficient. However, given Challenge 2 findings, this incremental IC also reflects trend-following, not genuine microstructure alpha.

## Challenge 5: Conditional TXFD6 IC (MAY)

Filtered TXFD6 ticks where all 5 levels of bids and asks have non-zero prices.

### TXFD6 Conditional IC (L5 coverage = all levels populated)

| Horizon | Conditional IC | NW t | Notes |
|---------|---------------|------|-------|
| 5s | +0.011 | 0.55 | Insignificant |
| 30s | +0.007 | 0.16 | Insignificant |

L5 coverage varies wildly: 100% on recent days (Mar 16-23), 0% on Mar 13, 5.6% on Mar 06. Even on full-coverage days, IC is near zero.

**Assessment**: Confirmed -- MLOFI has no predictive power on TXFD6 regardless of L5 data quality.

## Corrected Realized Spread (Bug 3 Fix)

Pro-cyclical direction: positive MLOFI = BUY, profit = mid_future - mid_at_fill.

### 2330 Realized Spread at 30s (price units x10000)

| Date | N fills | Mean RS | Median RS |
|------|---------|---------|-----------|
| 2026-03-24 | 26,076 | +4,329 | 0 |
| 2026-03-23 | 23,373 | +5,173 | 0 |
| 2026-03-20 | 20,532 | +3,294 | 0 |
| 2026-03-19 | 22,342 | +3,676 | 0 |
| 2026-03-18 | 16,629 | +2,990 | 0 |
| 2026-03-17 | 18,976 | +3,842 | 0 |
| 2026-03-16 | 23,086 | +3,554 | 0 |
| 2026-03-13 | 26,378 | +5,489 | 0 |
| 2026-03-12 | 24,671 | +3,502 | 0 |
| 2026-03-11 | 27,125 | +5,081 | 0 |
| 2026-03-10 | 34,952 | +5,509 | 0 |
| 2026-03-09 | 55,012 | +4,977 | 0 |
| 2026-03-06 | 27,153 | +2,087 | 0 |
| 2026-03-05 | 38,367 | +5,559 | 0 |
| 2026-03-04 | 46,296 | +4,822 | 0 |
| 2026-03-03 | 39,194 | +3,931 | 0 |
| 2026-03-02 | 30,693 | +6,014 | 0 |

Mean RS ~ +4,340 (0.434 NTD in price units). All 17 days positive.

**Caveat**: Median is 0 on all days -- the majority of "fills" at threshold=0.5 see zero mid-price movement at 30s. The positive mean is driven by tail moves in the signal direction, which is again consistent with trend-following (large moves continue).

## TXFD6 Baseline (Unchanged)

| Metric | Value |
|--------|-------|
| Pooled IC(30s) | -0.019 |
| NW t(30s) | -1.68 |
| Regression alpha CV | 337% |
| Kill gate | PASS by |IC| but WRONG SIGN |

TXFD6 MLOFI is noise. Coefficient instability (CV=337%) and wrong-sign IC confirm no signal.

## Updated Kill Gate Assessment

| Criterion | Stage 2a Value | Stage 2b Value | Verdict |
|-----------|---------------|----------------|---------|
| IC(30s) overlapping | +0.206 | +0.206 | PASS (unchanged) |
| IC(30s) non-overlapping | N/A | **+0.134** | PASS (NW t=19.51) |
| IC(30s) detrended | N/A | **-0.032** | **FAIL** (wrong sign) |
| IC(60s) detrended | N/A | **+0.025** | Marginal (t=2.21) |
| L1-only IC(30s) | N/A | +0.217 | L1 alone is better |
| Deep (L2-L5) IC(30s) | N/A | +0.029 | Marginal |
| OOS incremental IC(30s) | N/A | +0.119 | PASS but trend-driven |
| Regression alpha CV | 56% | 56% | WARNING (>50%) |

## Diagnosis: Why IC Was Inflated

1. **Overlapping returns**: 35% IC inflation from autocorrelated overlapping 30s returns. Non-overlapping IC=0.134 vs overlapping IC=0.206.

2. **Trend contamination**: The EMA(8) MLOFI is a slow-moving signal that tracks the local 5-min trend. When mid price trends up, MLOFI is positive AND 30s forward return is positive. This is not alpha -- it is lagged momentum.

3. **L1 dominance**: The "multi-level" part (L2-L5) adds almost nothing. L1 imbalance alone achieves IC=0.217, higher than the full MLOFI. The deep book information is noise on 2330.

4. **Scale**: IC monotonically increasing from 250ms to 60s is the hallmark of trend contamination. Genuine microstructure alpha should peak at a specific horizon and decay.

## Recommendation: TERMINATE

**Verdict**: The MLOFI microprice correction on 2330 is **NOT genuine microstructure alpha**. It is redundant trend-following that:

1. Flips to negative IC when local trends are removed (detrended IC at 30s = -0.032)
2. Is entirely driven by L1 imbalance (deep book adds nothing)
3. Has monotonically increasing IC with horizon (trend signature)
4. Was inflated 35% by overlapping returns

The signal passes the non-overlapping kill gate (IC=0.134, t=19.51) only because it captures the same information as any momentum/trend indicator. There is no microstructure edge.

**On TXFD6**: Confirmed dead (IC near zero, wrong sign, CV=337%).

**Actionable pivot if desired**: The L1 imbalance itself (IC=+0.148 at 5s, IC=+0.217 at 30s) warrants investigation as a simple momentum feature on 2330, but this is a well-known signal (already in FeatureEngine as `ofi_l1`). No novel alpha here.

## Files Modified

- `research/alphas/mlofi_microprice/impl.py` -- Bug 1 fix (pre-allocated price arrays)
- `research/alphas/mlofi_microprice/backtest_ic.py` -- Bugs 2-3 fixes + 5 new analysis functions

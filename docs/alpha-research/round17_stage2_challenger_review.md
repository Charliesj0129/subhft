# Round 17 Stage 2: Challenger Review — TSMC (2330) -> TMFD6 Lead-Lag IC

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Verdict**: **REJECT** -- statistical significance is absent; cost viability is marginal at best

---

## Challenge 1: Massive Overlapping Window Bias Inflates IC Significance (CRITICAL)

### The Problem

The prototype computes IC at 1-second sampling frequency with LB=300s and H=600s windows. This means:
- Each observation overlaps with the next by 599/600 = 99.83%
- Raw N = 31,382 but **effective independent observations = 52** (N_raw / max(LB, H))
- The reported t-stats and p-values from `scipy.spearmanr` assume N=31,382 independent observations -- they are **off by a factor of ~600x**

### Verification

I re-ran the IC calculation with non-overlapping sampling (every 600s):

| Config | IC (overlapping, N=31K) | IC (non-overlapping, N=52) | p-value (non-overlapping) |
|--------|------------------------|---------------------------|--------------------------|
| LB=300 H=300 | +0.056 | +0.070 | **0.477** |
| LB=300 H=600 | +0.065 | +0.119 | **0.402** |
| LB=60 H=600 | +0.049 | +0.073 | **0.605** |

**No configuration achieves p < 0.40 with proper non-overlapping sampling.** The "best" signal (LB=300, H=600) has p=0.40 on 52 observations -- completely insignificant. The IC point estimates actually increase with non-overlapping sampling, but this is expected with tiny N and high variance.

### The Prototype's `newey_west_tstat` Is Inadequate

The prototype implements a Newey-West correction with **lag=1** over **3 daily IC values**. This is doubly wrong:
1. With only 3 data points, any variance estimator is meaningless
2. The correct lag for LB=300, H=600 at 1-second sampling should be ~600, not 1

### Conclusion

The IC=0.065 headline number is a **point estimate with no statistical significance**. We cannot distinguish it from zero.

---

## Challenge 2: TMFD6 Self-Prediction Explains Most of the Signal (CRITICAL)

### The Problem

The results document interprets the positive IC as "TSMC leads TMFD6." An alternative explanation: both TSMC and TMFD6 track the TAIEX index, so what looks like TSMC->TMFD6 lead-lag is actually just **index momentum / autocorrelation**. If TMFD6's own past returns predict its future returns equally well, then TSMC adds nothing.

### Verification

Non-overlapping IC comparison:

| Config | TMFD6 self-prediction IC | 2330 lead IC | 2330 incremental value |
|--------|------------------------|-------------|----------------------|
| LB=300 H=300 | +0.037 (p=0.71) | +0.070 (p=0.48) | Unclear (both insignificant) |
| LB=300 H=600 | +0.032 (p=0.82) | +0.119 (p=0.40) | Unclear (both insignificant) |
| LB=60 H=600 | +0.024 (p=0.87) | +0.073 (p=0.61) | Unclear (both insignificant) |

TMFD6 self-prediction IC is lower than 2330 lead IC in all configs. This is mildly encouraging -- it suggests 2330 may carry incremental information beyond pure index momentum. **However, the difference is not statistically significant** (both p-values > 0.40). With 52 observations, we cannot distinguish "TSMC leads the index" from "both are noisy index momentum proxies."

---

## Challenge 3: Only 3 Days (One Partial) -- Insufficient Sample (HIGH)

### The Problem

- 3 calendar days: Mar-20, Mar-23, Mar-24
- Mar-23 is partial (2330 data ends at 10:51, only 6,354 bars vs ~13K for full days)
- Non-overlapping at 600s step: **9 observations** on Mar-23, **20 each** on Mar-20/24

### Per-Day Non-Overlapping IC (LB=300, H=600)

| Day | IC | p-value | N (independent) |
|-----|-----|---------|-----------------|
| Mar-20 | +0.081 | 0.734 | 20 |
| Mar-23 | +0.321 | 0.400 | 9 |
| Mar-24 | +0.328 | 0.158 | 20 |

Not a single day achieves p < 0.15. Mar-23's high IC (+0.321) is based on **9 observations** -- statistically meaningless.

### Minimum Data Requirement

For a directional signal at 600s horizon with expected IC ~0.05-0.10, a two-sided Spearman test at alpha=0.05 and power=0.80 requires approximately **N=80-300 independent observations**. That translates to **8-30 full trading days** (each day yields ~25 non-overlapping 600s windows in a 4.5-hour session). We have **49 total** (summing the 3 days). This is well below the minimum threshold.

---

## Challenge 4: Mar-20 LB=300 H=300 IC=0.209 -- Autocorrelation Artifact (MEDIUM)

The results document itself flags this as suspicious. With non-overlapping sampling, the per-day IC values are noisy but the concern is confirmed: at LB=H=300s, the lookback window for observation at time t ends exactly where the forward window begins. While this is not overlapping in the leakage sense (no shared data between signal and target), it does create a situation where:

1. The signal captures the last 300s price trend
2. The target captures the next 300s price trend
3. Any momentum persistence at the 300s scale will appear as positive IC

This is **genuine momentum**, not a bug -- but it is not specific to the TSMC->TMFD6 cross-asset relationship. It is index-level serial correlation, as confirmed by Challenge 2.

---

## Challenge 5: Cost Viability Is Marginal (HIGH)

### Expected Return Analysis

Using top/bottom quintile long-short decomposition on the pooled (overlapping) data:

| Metric | Value |
|--------|-------|
| Top quintile (2330 up big) mean TMFD6 fwd return | -1.16 bps |
| Bottom quintile (2330 down big) mean TMFD6 fwd return | -5.35 bps |
| Long-short spread | 4.19 bps |
| Per-side expected edge | 2.10 bps |
| RT cost (TMFD6) | 1.33 bps |
| **Net edge per trade** | **0.77 bps** |

### Problems

1. The **top quintile mean return is negative** (-1.16 bps). The long-short spread comes entirely from the bottom quintile being more negative. This is not a clean momentum signal -- it looks more like "everything drifts down, but less so after TSMC rises."

2. Net edge of 0.77 bps per trade is razor-thin. With estimation uncertainty (IC is not statistically significant), the true edge could easily be zero or negative.

3. This 0.77 bps assumes perfect signal execution at mid-price. Real execution includes:
   - Slippage (crossing the spread or partial fills)
   - Latency (36ms Shioaji RTT means the 600s forward window is already 0.06% consumed before you enter)
   - Signal decay within the 600s holding period

4. At 0.77 bps net edge and ~25 non-overlapping signals per day (one every 600s), daily expected PnL = 25 * 0.77 bps * 300,000 NTD (contract value) = ~58 NTD/day. This is not economically meaningful.

---

## Summary of Challenges

| # | Challenge | Severity | Impact |
|---|-----------|----------|--------|
| 1 | Overlapping window bias: all p-values > 0.40 | CRITICAL | IC=0.065 is statistically indistinguishable from zero |
| 2 | TMFD6 self-prediction partially explains signal | CRITICAL | Cannot confirm TSMC->TMFD6 lead-lag vs index momentum |
| 3 | Only 49 independent observations across 3 days | HIGH | Need 8-30 full days minimum |
| 4 | Mar-20 LB=300 H=300 IC=0.209 is momentum artifact | MEDIUM | Not cross-asset specific |
| 5 | Net edge ~0.77 bps before slippage/latency | HIGH | Economically unviable |

---

## Verdict: REJECT

The headline IC=0.065 is a point estimate with **zero statistical significance** (p=0.40 with proper non-overlapping sampling, N=52). The sample size of 3 days is far below the minimum required to validate a directional signal at 600s horizon. Even if the signal were real, the expected net edge of 0.77 bps per trade (before slippage) is economically unviable on TMFD6.

### What Would Change This to CONDITIONAL

1. **Collect 20+ full trading days** of synchronized 2330 + TMFD6 L1 data
2. **Achieve p < 0.05** on non-overlapping IC with Newey-West correction at proper lag
3. **Demonstrate incremental IC** of 2330 lead over TMFD6 self-prediction in a multivariate regression (partial correlation > 0.05, significant)
4. **Show net edge > 3 bps** per trade after realistic slippage and latency assumptions (need IC > 0.15 or combine with complementary signals)

Until these conditions are met, this signal should not proceed to strategy prototyping.

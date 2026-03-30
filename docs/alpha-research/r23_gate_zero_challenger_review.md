# R23 Gate Zero — Challenger Review

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Scope**: Gate Zero results — detrended autocorrelation on TMFD6/TXFD6 at 15min-4h

---

## Overall Verdict: CONDITIONAL PASS — but with a CRITICAL methodological concern (GZ-C1) that must be resolved before Stage 2

The IC magnitudes (-0.20 to -0.36) are extraordinary — 10-18x the kill threshold. This should make us MORE suspicious, not less. In 22 prior rounds, no signal this strong has survived scrutiny. I raise 4 challenges, one of which (GZ-C1) could explain the entire result as an artifact.

---

## GZ-C1: The Detrending Creates Mechanical Mean Reversion (CRITICAL)

This is the most important challenge. The detrending methodology has a subtle but potentially fatal flaw.

### The Problem

The script computes detrended returns as:

```python
detrended[i] = ret[i] - rolling_mean(ret[i-2:i+3])
```

where `rolling_mean` uses a 5-element centered window (lines 96-110 of `r23_gate_zero.py`). Then it measures lag-1 autocorrelation of the detrended series: `corr(detrended[i], detrended[i+1])`.

**The detrended value at time `i` includes `ret[i+1]` and `ret[i+2]` in its rolling mean window (centered, pad=2).** Similarly, `detrended[i+1]` includes `ret[i]` in its window. This creates a MECHANICAL negative correlation:

- If `ret[i]` is large and positive, the rolling mean around `i` increases, so `detrended[i]` is pulled DOWN (positive raw return minus elevated mean = smaller detrended value)
- But `ret[i]` also appears in the rolling mean for `detrended[i-1]` and `detrended[i+1]`, elevating THEIR rolling means too, which pushes `detrended[i+1]` DOWN

The centered rolling mean creates look-ahead contamination. The detrended series at time `i` is constructed using FUTURE values `ret[i+1]` and `ret[i+2]`, which mechanically introduces negative autocorrelation because:

```
detrended[i] = ret[i] - (1/5)(ret[i-2] + ret[i-1] + ret[i] + ret[i+1] + ret[i+2])
detrended[i+1] = ret[i+1] - (1/5)(ret[i-1] + ret[i] + ret[i+1] + ret[i+2] + ret[i+3])
```

Both share `ret[i]`, `ret[i+1]`, `ret[i+2]` in their rolling mean denominators. When `ret[i]` is large, it inflates the mean for both, pushing `detrended[i]` down and also partially affecting `detrended[i+1]`. This is a well-known artifact in time-series analysis: **centered moving average subtraction induces negative autocorrelation in the residuals.**

### Expected Artifact Magnitude

For a centered moving average of window `w` applied to an IID series, the lag-1 autocorrelation of the residuals is approximately `-1/(w-1)` = `-1/4` = `-0.25`. This is remarkably close to the observed IC of -0.235 (15min TMFD6) and -0.238 (15min TXFD6).

The fact that IC increases with horizon (from -0.24 at 15min to -0.36 at 2h) could also be an artifact: at longer horizons, N is smaller, so the rolling mean is more influenced by each individual observation, and boundary effects (lines 106-108, where the window shrinks at edges) become proportionally more severe.

### Required Resolution

**This is the MOST IMPORTANT test**: Re-run Gate Zero with CAUSAL (backward-looking only) detrending:

```python
# Replace centered rolling mean with LAGGED-ONLY mean
rolling_mean[i] = mean(ret[i-4:i])  # uses ONLY past values, not future
```

Or equivalently, use an EMA with a decay that does not reference future values. If the IC drops to zero with causal detrending, the entire result is an artifact. If it persists at |IC| >= 0.020, the mean reversion is genuine.

**Alternative validation**: Compute lag-1 autocorrelation of RAW (non-detrended) returns. If raw autocorrelation is also significantly negative, the mean reversion is real regardless of detrending method.

---

## GZ-C2: Pooled IC Cross-Day Contamination (HIGH)

Lines 200-205 of the script concatenate all detrended returns across days into a single array, then compute lag-1 autocorrelation on the concatenation:

```python
all_detrended = np.array(all_detrended_rets)
pooled_ic = rank_ic(all_detrended[:-1], all_detrended[1:])
```

This means the LAST return of day N is paired with the FIRST return of day N+1 as a lag-1 pair. But there is an overnight gap between them (potentially 16+ hours). The last return of a day and the first return of the next day have no causal relationship at the 15min-2h timescale.

The reported results table uses "Mean IC" (per-day average), not pooled IC, so this may not affect the headline numbers. But it is a methodological error that should be noted.

**Required**: Confirm that the headline IC numbers in the results table come from per-day averaging (lines 213-214), NOT from the pooled computation (lines 200-205). If per-day averaging, GZ-C2 is non-blocking.

---

## GZ-C3: 20-Day Sample with Regime Change (MEDIUM)

The data spans Jan 26 - Mar 26 (TMFD6, 20 days) and Jan 26 - Mar 24 (TXFD6, 13 days). This period includes:

1. **Jan/Feb**: TMFD6 median spread = 7 pts (wide-spread regime, anomalous)
2. **March**: TMFD6 median spread = 3 pts (1-tick, normal regime)

R14 and R16 established that Jan/Feb and March behave very differently. If the mean-reversion signal is primarily driven by the wide-spread Jan/Feb period (where larger price swings = more reverting), it may not persist in the normal March regime.

**Required**: Report the IC separately for:
- Jan/Feb days (wide spread)
- March days (tight spread)

If IC is strong in both sub-periods, this concern is resolved. If IC is primarily Jan/Feb, the signal is regime-dependent and less reliable.

---

## GZ-C4: The Cost Viability Estimate Is Sloppy (MEDIUM)

The results document estimates:
- "Expected edge per trade: ~IC * mean_abs_return = 0.30 * 20 bps = ~6 bps"

This arithmetic is wrong. IC (Spearman rank correlation) is NOT the fraction of return variance explained. The relationship between rank IC and expected profit is:

```
E[profit] ~ IC * sigma * sqrt(2/pi)
```

where sigma is the return standard deviation (not mean absolute return). Furthermore, IC = -0.30 means the SIGNAL (lagged return) has rank correlation -0.30 with the TARGET (forward return). The expected profit per trade depends on how many trades you take, your threshold for entry, fill rate, and slippage — not just IC * returns.

This is a non-blocking concern but the "~6 bps edge" and "~4.8 bps net" numbers should not be cited in Stage 2 without proper backtest.

---

## Summary

| # | Challenge | Severity | Status |
|---|-----------|----------|--------|
| GZ-C1 | Centered rolling mean creates mechanical negative autocorrelation | CRITICAL | Must re-run with causal detrending |
| GZ-C2 | Pooled IC cross-day contamination | HIGH | Likely non-blocking if headline uses per-day mean |
| GZ-C3 | Jan/Feb vs March regime split needed | MEDIUM | Non-blocking but important for Stage 2 |
| GZ-C4 | Cost viability arithmetic is wrong | MEDIUM | Non-blocking |

---

## Conditions for Approval

**If GZ-C1 is resolved** (IC >= 0.020 with causal detrending or with raw returns), then:

- **STRONG PASS** — proceed to Stage 2 with mean-reversion strategy prototype
- This would be the first genuine, strong, empirically-validated signal in 23 rounds
- The finding that TMFD6/TXFD6 revert at 15min-2h is important and actionable

**If GZ-C1 fails** (IC drops to zero with causal detrending):

- **FAIL** — the entire result is a statistical artifact of centered moving average residuals
- The IC of ~-0.25 matching the theoretical artifact magnitude (-1/4) would confirm this
- Gate Zero remains failed, Candidate A is killed

The resolution requires re-running the script with one change (causal window) or computing raw return autocorrelation. This is a 10-minute test.

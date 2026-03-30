# Round 18 Stage 2a: HMM Data Diagnostic Results

**Date**: 2026-03-26
**Status**: KILLED -- states indistinguishable (separation ratio 0.024)

## Result: HMM-RCM is DEAD

### Kill Gate 1: State Distinguishability -- FAIL

HMM Parameters (2-state Gaussian, manual Baum-Welch, 100 iterations):
- State 0: mu = -0.0000, sigma = 0.0000 (degenerate -- collapsed to single point)
- State 1: mu = +1.559, sigma = 31.917

|mu_1 - mu_0| = 1.559 pts
2 * max(sigma) = 63.834 pts
**Separation ratio: 0.024** (need >= 1.0)

The HMM collapsed into a trivial solution: one degenerate state absorbing near-zero returns, and one broad state absorbing everything else. This is a classic failure mode when the underlying data does NOT have a bimodal return distribution.

### Kill Gate 2: OOS Trade Count -- PASS (trivially)

360 of 361 OOS bars classified as "trending" (P > 0.7). The HMM is not discriminating -- it puts everything in one state. This passes the N >= 100 gate but is meaningless.

### Momentum Trade PnL: FAIL

- N = 352 trades with measurable next-bar return
- Mean return: -0.92 pts (NEGATIVE after always going with momentum)
- Win rate: 54.0% (barely above 50%)
- After cost (3.92 pts RT): net negative

### Root Cause

1. **TMFD6 5-min returns are unimodal**. The return distribution does not have distinct regimes -- it's a single Gaussian with fat tails. A 2-state HMM cannot find meaningful structure.

2. **413 IS observations is marginal** for HMM fitting. With 16 days and ~50 bars/day, the model has only 8 days of training data. Baum-Welch converges to a degenerate solution.

3. **The state 0 collapse to zero sigma** indicates the EM algorithm found a local optimum where one state captures a handful of exact-zero returns (from bars where mid_price didn't change), rather than learning meaningful regimes.

4. **State persistence is wrong-directional**. State 1 lasts 33 min on average, State 0 lasts 9 min. A useful regime model would have states lasting 30-120 min each.

### Data Quality Note

Same issue as VRB: only **16 day-session dates** available, not 58. 8-day IS / 8-day OOS split gives only 413 / 361 5-min bar returns respectively. This is not enough for robust HMM calibration.

## Diagnostic Scripts

- `research/experiments/validations/vrb_diagnostic/hmm_diagnostic.py`
- `research/experiments/validations/vrb_diagnostic/hmm_results.json`

## Cost Model Used

3.92 pts = 1.19 bps RT (per Challenger correction)

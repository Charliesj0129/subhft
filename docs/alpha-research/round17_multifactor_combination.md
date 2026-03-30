# Round 17 Direction E: Multi-Factor Combination IC

**Date**: 2026-03-26
**Script**: `research/experiments/validations/tsmc_leadlag/multifactor_ic.py`
**Data**: 22 days, ~900 non-overlapping observations per horizon

---

## Kill Gate: FAIL

Combined IC does NOT beat max(single IC) + 0.01 at either horizon. Multi-factor combination does not justify its complexity.

---

## Factors Tested

| Factor | Description | H=120s IC | H=300s IC |
|--------|-------------|-----------|-----------|
| 2330_ret_300s | TSMC 5-min return | **+0.046** | **+0.045** |
| tmf_imbalance | TMFD6 (bid-ask)/(bid+ask) | +0.024 | -0.026 |
| tmf_self_300s | TMFD6 own 5-min return | -0.007 | -0.011 |

2330_ret_300s is the strongest single factor at both horizons. TMF imbalance switches sign between horizons (unreliable). TMF self is near zero.

---

## Combination Methods (H=120s)

| Method | IC | p-value | Notes |
|--------|-----|---------|-------|
| 2330 alone | +0.046 | 0.167 | Baseline |
| Equal-weight z-score (all 3) | +0.038 | 0.248 | WORSE than 2330 alone |
| 2330 + imbalance only | +0.051 | 0.123 | Marginally better |
| Ridge regression (in-sample) | -0.005 | 0.886 | Overfits/useless |
| LOO-CV Ridge | -0.026 | n/a | Negative OOS — confirms overfitting |

## Combination Methods (H=300s)

| Method | IC | p-value | Notes |
|--------|-----|---------|-------|
| 2330 alone | +0.045 | 0.180 | Baseline |
| Equal-weight z-score (all 3) | +0.012 | 0.716 | MUCH WORSE |
| 2330 + imbalance only | +0.018 | 0.600 | Worse |
| Ridge regression (in-sample) | +0.041 | 0.228 | Marginal |
| LOO-CV Ridge | +0.023 | n/a | Slightly positive but below 2330 alone |

---

## Sign-Agreement Analysis

When 2330 return direction and TMF depth imbalance agree vs disagree:

| Condition | H=120s IC | H=300s IC | N |
|-----------|-----------|-----------|---|
| Agree | +0.083 | -0.004 | ~240 |
| Disagree | +0.025 | +0.068 | ~660 |

At H=120s, agreement between 2330 and imbalance boosts IC to +0.083 (from +0.046 baseline). But this only covers 27% of observations. At H=300s, the relationship flips -- agreement hurts IC.

**Interpretation**: The sign-agreement effect at H=120s is interesting but unreliable across horizons. The factors are measuring different things on different timescales and don't combine cleanly.

---

## Per-Day Factor IC Correlation

The 2330 and imbalance factors show LOW per-day correlation:
- Some days both positive (Feb-23, Mar-12, Mar-24)
- Some days opposite sign (Feb-06, Mar-03, Mar-20)
- No consistent pattern

This confirms they're somewhat independent signals, but their combination doesn't improve prediction because neither is strong enough individually.

---

## Verdict

**Multi-factor combination FAILS the kill gate.** 2330_ret_300s alone (IC=+0.046) is the best signal. Adding TMF imbalance or self-prediction either hurts or marginally helps depending on horizon, never exceeding the +0.01 improvement threshold.

**Reasons combination fails:**
1. Individual factor ICs are too weak (all < 0.05) — combining noisy signals doesn't magically improve SNR
2. Factor relationship is unstable across horizons (imbalance sign flips)
3. Ridge regression overfits on this sample size (N=900)
4. The factors are not complementary in a stable way — agreement helps at one horizon, hurts at another

**Recommendation**: Stick with 2330_ret_300s as a standalone signal or CBS filter. The additional complexity of multi-factor models is not justified by the data.

# R22 Stage 2 Diagnostic Report — 2026-03-28

## rv_ratio_regime — ADVANCES (narrowed scope)

### Gate Zero Results
| Horizon | Raw IC  | Detrended IC | p-value |
|---------|---------|-------------|---------|
| 30s     | +0.007  | +0.020      | <0.001  |
| 60s     | +0.010  | +0.006      | <0.001  |
| 120s    | +0.008  | +0.011      | <0.001  |
| 300s    | +0.018  | +0.031      | <0.001  |
| 600s    | +0.021  | +0.028      | <0.001  |

- **Non-degeneracy**: PASS (63.6% non-zero 5s returns)
- **Detrended IC**: All positive, NOT trend contaminated (detrended > raw at 30s/300s)
- **Abs-return monotonicity**: Q1=7.60 → Q5=11.80 pts at 60s (55% increase) — genuine vol regime detection
- **CBS conditioning**: p=0.19 — FAILS p<0.10 threshold (insufficient data, N=20 days)
- **OOS**: Per-day IC unstable (+0.24 to -0.27), pooled detrended IC positive

### Reviewer Verdicts
- **Challenger**: CONDITIONAL APPROVE — reframe as volatility regime feature, NOT CBS directional filter. CBS filter usage deferred until 60+ trading days available (p<0.05 power).
- **Execution**: APPROVE — clean implementation, all-positive detrended IC. Minor: dead code cleanup needed, warmup_min_events=2400 for FE integration.

### Consensus: ADVANCE as FeatureEngine v3 feature
- Feature: `vrr_5_300_x1000` at index [22]
- Scope: Volatility regime detector (absolute return predictor), NOT CBS directional gate
- CBS conditioning deferred until data accumulation (60+ days)
- Sign consistency check required: >60% across rolling 5-day windows

---

## imbalance_mr_speed — KILLED

### Gate Zero Results
| Horizon | Raw IC  | Detrended IC | p-value |
|---------|---------|-------------|---------|
| 60s     | +0.005  | **-0.021**  | <0.001  |
| 120s    | +0.006  | **-0.005**  | <0.001  |
| 300s    | +0.015  | +0.017      | <0.001  |
| 600s    | +0.031  | +0.013      | <0.001  |

- **Non-degeneracy**: PASS (CV=1.41)
- **Detrended IC**: NEGATIVE at 60s/120s — signal predicts wrong direction at proposed horizons
- **Incremental over ret_autocov**: YES (rho=-0.20, partial IC=+0.020 at 300s)
- **CBS conditioning**: p=0.64 — catastrophic failure
- **OOS**: Sign flips across days (-0.32 to +0.12) — random, not stable regime detector

### Reviewer Verdicts
- **Challenger**: REJECT — (1) negative detrended IC at proposed horizons, (2) random OOS instability, (3) CBS conditioning p=0.64
- **Execution**: CONDITIONAL APPROVE — but noted CBS FAIL, negative short-horizon IC, autocov formula bug, overflow concern

### Consensus: KILLED (Challenger REJECT is final per team rules)
Reason: Signal predicts wrong direction at its own proposed horizons. OU fit on TMFD6's thin book produces noise, not a stable regime detector.

---

## R22 Net Outcome

**1 feature addition**: `vrr_5_300_x1000` (rv_ratio_regime) → FeatureEngine v3 as volatility regime detector.
**0 new strategy signals**: CBS filter usage requires more data. No standalone alpha.
**1 killed**: imbalance_mr_speed — dead on TMFD6.

Meta-conclusion: Consistent with R20 finding that L1 microstructure signals are approaching exhaustion on TMFD6.

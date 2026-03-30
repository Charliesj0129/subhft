# Round 17 Stage 2: Expanded Validation — 2330 → TMF Lead-Lag

**Date**: 2026-03-26
**Data**: 22 trading days (Feb 6 - Mar 26, 2026), ~280K aligned 1-second bars
**Scripts**: `research/experiments/validations/tsmc_leadlag/{export_aligned_data,expanded_ic}.py`
**Method**: Non-overlapping windows, Spearman IC, day-level bootstrap CI

---

## Kill Gate Results: 2/4 PASS

| Gate | Criterion | Result | Verdict |
|------|-----------|--------|---------|
| 1. IC significance | IC >= 0.03, p < 0.05 | Best IC=+0.061, p=0.066 | **FAIL** (p barely misses) |
| 2. Sign consistency | Same sign >= 70% days | Best 68.2% (LB=120, H=60) | **FAIL** |
| 3. Incremental IC | 2330 IC over TMF self > 0.02 | +0.096 (LB=300, H=120) | **PASS** |
| 4. Net edge | Q5-Q1 edge > 3 bps after cost | +2.34 bps - 1.33 bps = +1.01 bps | **FAIL** (gate says > 3 net) |

---

## Non-Overlapping IC Table (all configs)

| LB | H | IC | p-value | N | Self IC | Incr IC | Sign% | 95% CI | Q5-Q1 bps |
|----|---|----|---------|---|---------|---------|-------|--------|-----------|
| 30 | 60 | -0.004 | 0.764 | 4612 | +0.012 | -0.012 | 45.5% | [-0.039, +0.018] | +0.58 |
| 30 | 120 | -0.004 | 0.862 | 2297 | -0.019 | +0.005 | 40.9% | [-0.058, +0.029] | +1.21 |
| 30 | 300 | -0.010 | 0.773 | 906 | +0.023 | -0.011 | 45.0% | [-0.063, +0.077] | -1.28 |
| 30 | 600 | +0.011 | 0.815 | 442 | +0.035 | +0.006 | 54.5% | [-0.096, +0.119] | +5.18 |
| 60 | 60 | -0.019 | 0.204 | 4602 | +0.001 | -0.019 | 54.5% | [-0.046, +0.015] | -0.71 |
| 60 | 120 | -0.028 | 0.174 | 2292 | -0.040 | -0.014 | 59.1% | [-0.069, +0.014] | -1.43 |
| 60 | 300 | -0.006 | 0.863 | 906 | +0.005 | +0.007 | 45.0% | [-0.057, +0.075] | +0.02 |
| 60 | 600 | +0.001 | 0.985 | 442 | +0.040 | +0.012 | 42.9% | [-0.093, +0.121] | +2.01 |
| 120 | 60 | -0.019 | 0.359 | 2288 | -0.026 | -0.015 | 68.2% | [-0.071, +0.021] | -0.57 |
| 120 | 120 | -0.008 | 0.718 | 2279 | -0.055 | +0.002 | 63.6% | [-0.064, +0.033] | -0.62 |
| 120 | 300 | -0.003 | 0.929 | 902 | -0.010 | +0.005 | 35.0% | [-0.051, +0.075] | -1.93 |
| 120 | 600 | +0.050 | 0.299 | 440 | -0.007 | +0.040 | 62.5% | [-0.086, +0.117] | +1.47 |
| **300** | **60** | **+0.013** | 0.708 | 904 | -0.006 | +0.024 | 66.7% | [-0.058, +0.080] | -0.03 |
| **300** | **120** | **+0.061** | **0.066** | 899 | -0.036 | **+0.096** | 57.1% | [-0.045, +0.088] | +2.34 |
| **300** | **300** | **+0.038** | 0.261 | 886 | +0.027 | +0.027 | 65.0% | [-0.039, +0.088] | +0.99 |
| **300** | **600** | **+0.056** | 0.248 | 431 | +0.040 | +0.034 | 56.2% | [-0.090, +0.116] | -1.67 |

### Key Observations

1. **LB=300s is the only lookback with consistently positive IC** across horizons. All shorter lookbacks are near zero or negative.

2. **Best config: LB=300s, H=120s (IC=+0.061, p=0.066)**. Falls just short of p<0.05 significance. The self-prediction IC is -0.036 (TMF past 300s return has slight NEGATIVE predictive power at 120s forward), while 2330 has +0.061. Incremental IC = +0.096.

3. **Bootstrap CI for LB=300s, H=120s: [-0.045, +0.088]**. CI includes zero.

4. **Sign consistency is low**: best is 68.2% (LB=120, H=60 with negative IC). For LB=300s configs, 57-67%.

5. **Quintile spread is narrow**: LB=300s H=120s shows Q5-Q1 = +2.34 bps. After 1.33 bps RT cost, net = +1.01 bps.

---

## Per-Day IC Detail (LB=300s, H=120s)

| Date | 2330 IC | Self IC | Direction |
|------|---------|---------|-----------|
| 2026-02-06 | +0.037 | -0.071 | + |
| 2026-02-23 | +0.161 | +0.008 | + |
| 2026-02-24 | -0.015 | +0.113 | - |
| 2026-02-25 | +0.016 | -0.130 | + |
| 2026-02-26 | -0.046 | -0.059 | - |
| 2026-03-03 | +0.044 | +0.077 | + |
| 2026-03-04 | -0.059 | -0.155 | - |
| 2026-03-05 | +0.052 | +0.077 | + |
| 2026-03-06 | +0.073 | +0.049 | + |
| **2026-03-09** | **-0.278** | -0.259 | **-** (outlier) |
| 2026-03-10 | +0.025 | +0.012 | + |
| 2026-03-11 | -0.095 | +0.029 | - |
| 2026-03-12 | +0.082 | +0.003 | + |
| 2026-03-13 | -0.026 | -0.112 | - |
| 2026-03-16 | -0.160 | -0.133 | - |
| 2026-03-17 | +0.289 | +0.278 | + |
| 2026-03-18 | +0.102 | +0.060 | + |
| 2026-03-20 | +0.207 | +0.090 | + |
| 2026-03-23 | -0.018 | +0.017 | - |
| 2026-03-24 | +0.152 | +0.198 | + |
| 2026-03-25 | -0.102 | -0.195 | - |

**Positive days**: 12/21 (57%). Not significantly above 50%.

**Notable**: Mar-09 is a strong outlier (IC=-0.278). Mar-17 and Mar-20 are strong positives but also have high self IC.

---

## Why the Initial 3-Day IC Was Inflated

The original Stage 2 prototype reported IC up to +0.065 on overlapping 1-second windows across 3 days. The expanded analysis shows:

1. **Overlapping windows**: IC computed on every 1-second bar with 300s lookback means 299/300 of the data points share the same lookback window. This creates massive autocorrelation that inflates IC significance.

2. **Small sample**: 3 days happened to include 2 of the strongest positive-IC days (Mar-20: +0.207, Mar-24: +0.152).

3. **Non-overlapping correction**: Sampling every 300s reduces N from ~32K to ~900, and the IC drops from "highly significant" to p=0.066.

---

## Verdict

**The 2330 → TMF lead-lag signal has REAL but INSUFFICIENT edge for standalone trading.**

- Incremental IC over TMF self-prediction is genuinely positive (+0.096 at best config)
- But the raw IC fails significance, sign consistency is low, and net edge after costs is only ~1 bps
- The signal is too noisy day-to-day for reliable live trading

### Possible salvage paths
1. **CBS confirmation filter**: only enter CBS when 2330 5-min return confirms direction (reduces false entries)
2. **Multi-factor combination**: combine 2330 lead with TMF self-prediction and LOB features
3. **Longer data accumulation**: 22 days is still short; 60+ days may improve significance
4. **TXFD6 as alternative target**: TX (large contract) may have cleaner lead-lag with higher liquidity

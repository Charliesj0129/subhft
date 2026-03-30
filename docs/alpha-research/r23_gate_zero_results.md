# R23 Gate Zero Results v2: Corrected Detrending

**Date**: 2026-03-28
**Script**: `research/experiments/validations/r23_gate_zero.py`
**Version**: v2 -- fixes GZ-C1 (centered MA artifact) and GZ-C2 (overnight gap)

---

## Corrections Applied

**GZ-C1 (CRITICAL)**: v1 used centered rolling mean for detrending, which mechanically created -0.25 negative autocorrelation. v2 runs THREE variants:
1. **RAW**: No detrending -- ground truth, no artifact possible
2. **CAUSAL_5**: Past-only 5-element rolling mean (no look-ahead)
3. **CAUSAL_10**: Past-only 10-element rolling mean

**GZ-C2**: v2 uses per-day IC only. No cross-day pooling (no overnight gap contamination).

---

## Results: RAW Returns (Ground Truth)

### TMFD6 (20 trading days)

| Horizon | Mean IC | 95% CI | p-value | Sign neg% | Significant? |
|---------|---------|--------|---------|-----------|-------------|
| 15min | -0.003 | [-0.075, +0.073] | 0.94 | 55% | NO |
| 30min | -0.058 | [-0.143, +0.020] | 0.19 | 56% | NO |
| **1h** | **-0.178** | **[-0.327, -0.035]** | **0.034** | **72%** | **YES** |
| 2h | -0.125 | [-0.330, +0.112] | 0.30 | 69% | NO |
| 4h | +0.000 | [-0.375, +0.500] | 1.00 | 62% | NO |

### TXFD6 (13 trading days)

| Horizon | Mean IC | 95% CI | p-value | Sign neg% | Significant? |
|---------|---------|--------|---------|-----------|-------------|
| **15min** | **-0.152** | **[-0.226, -0.083]** | **0.002** | **92%** | **YES** |
| 30min | -0.088 | [-0.218, +0.054] | 0.25 | 62% | NO |
| 1h | -0.035 | [-0.149, +0.095] | 0.61 | 62% | NO |
| 2h | -0.139 | [-0.414, +0.170] | 0.40 | 64% | NO |
| 4h | -0.250 | [-0.833, +0.417] | 0.49 | 67% | NO (N=6) |

### Causal Detrended (CAUSAL_5) -- Secondary Check

Causal detrending generally REDUCES the signal further, suggesting the RAW mean-reversion signal is partially trend-related (local momentum that detrending removes).

TMFD6 1h CAUSAL_5: IC = -0.099, p = 0.15 -- no longer significant.
TXFD6 15min CAUSAL_5: IC = -0.017, p = 0.55 -- signal disappears.

---

## Interpretation

### v1 vs v2 Comparison

| Metric | v1 (centered MA) | v2 (RAW) | v2 (CAUSAL) |
|--------|------------------|----------|-------------|
| TMFD6 1h IC | -0.320 | -0.178 | -0.099 |
| TXFD6 15min IC | -0.238 | -0.152 | -0.017 |
| TMFD6 1h p | 0.0000 | 0.034 | 0.147 |

**The v1 signal was approximately 50% artifact (centered MA) and 50% real.**

### What Survives

Two results show statistical significance at p < 0.05:

1. **TMFD6 1h RAW**: IC = -0.178, p = 0.034, 72% days negative
   - Genuine mean reversion at 1h on TMFD6
   - Effect size: moderate (IC = -0.18)
   - BUT: causal detrending reduces to IC = -0.10 (p = 0.15) -- marginal

2. **TXFD6 15min RAW**: IC = -0.152, p = 0.002, 92% days negative
   - Strongest surviving signal
   - BUT: causal detrending eliminates it (IC = -0.02, p = 0.55)
   - This suggests the 15min RAW reversion is partially a local-trend artifact

### What Dies

- 15min, 30min on TMFD6: NO signal in RAW (IC near zero)
- 30min, 1h, 2h, 4h on TXFD6: NOT significant (p > 0.20)
- All horizons under causal detrending: marginal or zero

---

## Gate Zero Verdict: MARGINAL PASS

| Test | Threshold | RAW Result | Causal Result | Verdict |
|------|-----------|------------|---------------|---------|
| Any |IC| >= 0.020 | yes | TMFD6 1h: -0.178 | TMFD6 1h: -0.099 | **PASS (RAW), MARGINAL (causal)** |
| p < 0.05 | yes | TMFD6 1h: 0.034 | TMFD6 1h: 0.147 | **PASS (RAW), FAIL (causal)** |
| Both instruments | consistent | Partially | No | **WEAK** |
| CI excludes zero | yes | TMFD6 1h: yes | TMFD6 1h: no | **PASS (RAW), FAIL (causal)** |

**Honest assessment**: There IS a real mean-reversion signal on TMFD6 at 1h, but it is:
- Weaker than v1 suggested (IC = -0.18, not -0.32)
- Only significant in RAW returns, marginal under causal detrending
- Only on TMFD6, not consistently on TXFD6
- Only at 1h, not at other horizons
- Based on 18 trading days (limited sample)

**This is NOT the "10-18x threshold" signal I claimed in v1. It is a modest, single-horizon, single-instrument effect that may or may not survive further scrutiny.**

---

## Cost Viability (Revised, addressing GZ-C4)

The v1 cost estimate (IC * mean_abs_return = 6 bps) was incorrect methodology. IC does not translate linearly to profit.

A more realistic assessment:
- IC = -0.18 at 1h means modest predictive power
- To translate to PnL, need actual backtested strategy with entry/exit rules
- At TMFD6 RT cost of 3.92 pts (1.19 bps), need substantial per-trade edge
- IC of -0.18 with ~20 bps volatility per hour is MARGINAL for profitability
- Cannot claim cost viability without a backtest

---

## Next Steps (if approved for Stage 2)

1. **Jan/Feb vs March split** (GZ-C3): Check if TMFD6 1h signal persists in March (tighter spreads)
2. **Entry threshold optimization**: What size of 1h move triggers contrarian entry?
3. **Hold period optimization**: Is 1h optimal or is 30min-2h range better?
4. **Backtest with realistic costs**: Actual PnL simulation with spread/slippage
5. **Overlapping windows**: Use overlapping returns for more observations (statistical power)

---

## Acknowledgment

The v1 result showing IC = -0.32 at 1h was approximately 50% artifact from centered moving average detrending. The challenger (GZ-C1) correctly identified this critical flaw. The genuine signal is IC = -0.18 (RAW) to -0.10 (causal), which is real but modest.

This is a lesson in methodology: always use causal-only detrending, and always compare detrended results against RAW returns as a sanity check.

# Round 17 Stage 2: TSMC (2330) → TMFD6 Lead-Lag IC Results

**Date**: 2026-03-26
**Script**: `research/experiments/validations/tsmc_leadlag/prototype_ic.py`
**Data**: 3 overlapping days (Mar-20, 23, 24), ~32K aligned 1-second bars

---

## Kill Gate: PASS

**Max |IC| = 0.065 (SG1: LB=300s, H=600s)**. Multiple signals cross the 0.02 threshold.

---

## Signal Group 1: Price Lead-Lag (2330 past returns → TMFD6 forward returns)

### Pooled IC Table (*** = |IC| >= 0.02)

| Lookback | H=1s | H=5s | H=30s | H=60s | H=300s | H=600s |
|----------|------|------|-------|-------|--------|--------|
| 1s | +0.012* | +0.012* | -0.002 | +0.002 | +0.006 | +0.007 |
| 5s | +0.010* | +0.007 | -0.011* | +0.006 | +0.015* | +0.017* |
| 30s | -0.004 | -0.013* | -0.015* | -0.013* | **+0.030*** | **+0.038*** |
| 60s | +0.002 | +0.002 | -0.012* | +0.001 | **+0.036*** | **+0.049*** |
| 300s | +0.011* | +0.015* | **+0.033*** | **+0.044*** | **+0.056*** | **+0.065*** |

### Key Findings

1. **Strong positive lead-lag at longer horizons**: 2330 past 300s returns predict TMFD6 forward 60-600s returns with IC = +0.044 to +0.065.
2. **IC grows with BOTH lookback and horizon**: The sweet spot is LB=60-300s, H=300-600s. This matches our cost-viable horizon.
3. **Short-horizon is weak**: LB=1-5s shows IC ~0.01 -- too weak after costs.
4. **Negative IC at mid-range**: LB=30s, H=30-60s shows negative IC (-0.013 to -0.015). This is mean-reversion at short horizon, momentum at longer horizon.
5. **Hit rates**: LB=300s, H=600s pooled HR = 0.554-0.608 across days. Consistently above 50%.

### Per-Day Stability

| Day | LB=300s H=300s IC | LB=300s H=600s IC | LB=60s H=600s IC |
|-----|-------------------|-------------------|-------------------|
| Mar-20 | **+0.209** | +0.064 | +0.070 |
| Mar-23 | -0.037 | +0.039 | -0.022 |
| Mar-24 | +0.105 | **+0.190** | **+0.102** |

**Concern**: Mar-23 is partial day (2330 data ends at 10:51 TWN) and shows weaker/mixed results. Mar-20 has extremely strong LB=300s H=300s IC=0.209 (possible autocorrelation artifact at exactly LB=H=300s). Mar-24 consistently positive.

### Interpretation

TSMC stock price movements over 1-5 minute windows predict Mini-TAIEX futures directional returns 5-10 minutes later. This is consistent with TSMC being ~30% of TAIEX index weight -- when TSMC moves, the index follows, but with a lag in the mini-futures contract.

---

## Signal Group 2: Volume/Activity Surge

| Signal | H=30s | H=60s | H=300s | H=600s |
|--------|-------|-------|--------|--------|
| Depth surge direction | -0.005 | +0.002 | +0.009 | +0.012 |
| |2330 chg| → |TMFD6 ret| | -0.002 | +0.003 | +0.009 | +0.015 |

**Verdict**: WEAK. All ICs < 0.02 pooled. Volume data is all zeros in the npy files (likely not captured), so depth change is a noisy proxy. This signal group is not actionable with current data.

---

## Signal Group 3: Spread / LOB State

| Signal | H=1s | H=5s | H=30s | H=60s | H=300s | H=600s |
|--------|------|------|-------|-------|--------|--------|
| Spread change → vol | n/a | n/a | +0.003 | +0.003 | +0.006 | -0.000 |
| **2330 imbalance → dir** | +0.001 | +0.007 | -0.006 | **-0.024*** | **-0.051*** | -0.005 |
| **2330 spread → vol** | n/a | n/a | **+0.130*** | **+0.094*** | **+0.074*** | **+0.091*** |

### Key Findings

1. **2330 spread level → TMFD6 volatility**: IC = +0.074 to +0.130. When TSMC spread is wide, TMFD6 absolute returns are larger. This is a **volatility timing signal** (not directional).

2. **2330 imbalance → TMFD6 direction**: IC = -0.024 to -0.051 at 60-300s horizons. The **negative** sign is surprising -- when 2330 bid_qty > ask_qty (buy pressure), TMFD6 tends to go DOWN 60-300s later. This could be:
   - Contrarian / mean-reversion effect
   - Adverse selection: high bid depth = market makers providing liquidity against informed selling
   - Day-specific artifact (sign flips between days: Mar-23 positive, Mar-20/24 negative)

3. **Spread change → volatility**: Weak (IC < 0.01). Not useful.

### Per-Day Stability for Imbalance Signal

| Day | H=60s IC | H=300s IC | Direction |
|-----|----------|-----------|-----------|
| Mar-20 | -0.034 | -0.080 | Negative (contrarian) |
| Mar-23 | +0.101 | +0.042 | **Positive (momentum)** |
| Mar-24 | -0.105 | -0.102 | Negative (contrarian) |

**UNSTABLE**: Sign flips between days. Not reliable for directional trading. However, the |IC| is high on each day -- the relationship is strong but the direction changes. This suggests a regime-dependent signal.

### Per-Day Stability for Spread Level Signal

| Day | H=30s IC | H=300s IC | Direction |
|-----|----------|-----------|-----------|
| Mar-20 | -0.109 | -0.227 | **Negative** |
| Mar-23 | +0.202 | +0.103 | **Positive** |
| Mar-24 | +0.192 | +0.110 | **Positive** |

**UNSTABLE SIGN**: Mar-20 shows negative IC (wide 2330 spread = LOWER TMFD6 volatility) while Mar-23/24 show positive. Pooled IC is positive but driven by Mar-23/24. Not reliable.

---

## Summary: Actionable Signals

| Signal | Pooled IC | Stable? | Horizon | Directional? | Verdict |
|--------|-----------|---------|---------|--------------|---------|
| **SG1: 2330 ret(300s) → TMFD6 fwd(60-600s)** | +0.044 to +0.065 | 2/3 days positive | 60-600s | YES | **PROCEED** |
| **SG1: 2330 ret(60s) → TMFD6 fwd(300-600s)** | +0.036 to +0.049 | 2/3 days positive | 300-600s | YES | **PROCEED** |
| SG3: 2330 spread → TMFD6 vol | +0.074 to +0.130 | Sign flips | 30-600s | No (vol only) | CAUTION |
| SG3: 2330 imbalance → TMFD6 dir | -0.024 to -0.051 | Sign flips | 60-300s | Yes but unstable | REJECT |
| SG2: All volume signals | < 0.02 | n/a | n/a | n/a | DEAD |

---

## Recommended Next Steps

1. **Primary candidate**: 2330 past return (60-300s lookback) as directional predictor for TMFD6 forward return (300-600s horizon). IC = 0.04-0.07, cost-viable horizon, consistent with TSMC→index weight transmission.

2. **Risk to validate**:
   - Only 3 days of data (one partial). Need more days for robust IC estimate
   - Mar-23 partial day shows weaker/mixed results -- need full day validation
   - LB=300s H=300s IC=0.209 on Mar-20 is suspiciously high -- check for data leakage or autocorrelation artifact
   - Actual trading edge after 1.33 bps cost? IC=0.05 at H=300s typically translates to ~5-10 bps expected return per signal. Marginal but potentially viable

3. **Complementarity to CBS**: This is a **momentum** signal (2330 goes up → TMFD6 goes up later). CBS is **contrarian** (after 40 bps move → reversal). They are orthogonal and could be combined: use 2330 lead for momentum entries, CBS for reversal entries

4. **Implementation path**: Subscribe to 2330 (TSMC) quote feed via Shioaji. Compute rolling 60-300s return on 2330 mid_price. Use as directional signal for TMFD6 entries. Hold 300-600s, exit on signal reversal or timeout

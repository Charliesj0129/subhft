# Round 17 -- OIDS Cost Model Sensitivity Analysis

**Date**: 2026-03-26
**Analyst**: Claude (Execution reviewer agent)
**Scope**: IC-to-edge conversion, minimum IC thresholds, kill gate calibration for OIDS on TMFD6

---

## 1. TMFD6 Absolute Return Profile by Horizon

Data source: Round 16 MC-7 analysis (TMFD6, 7 March trading days, 3.18M ticks).

The MC-7 table provides measured median and mean absolute moves up to 300s. OIDS targets 5-60 minute horizons, so we must extrapolate beyond the measured data. For diffusive price processes, absolute return scales approximately as sqrt(t). We use the 300s measured values as anchor and extrapolate.

### Measured Data (MC-7, TMFD6 March 2026)

| Horizon | Median Move (pts) | Mean Move (pts) | P95 Move (pts) |
|---------|-------------------|-----------------|----------------|
| 5s      | 3.5               | 5.1             | 14.5           |
| 30s     | 9.0               | 12.8            | 36.5           |
| 60s     | 13.0              | 18.3            | 52.0           |
| 120s    | 18.5              | 26.4            | 74.5           |
| 300s    | 30.0              | 42.2            | 120.5          |

### Extrapolated to OIDS Horizons (sqrt-scaling from 300s anchor)

Using mean move at 300s = 42.2 pts, sigma_300 = 42.2 pts.

Scaling: sigma(t) = sigma(300s) * sqrt(t / 300)

| Horizon | Seconds | sqrt(t/300) | Est. Mean Abs Move (pts) | Est. Mean Abs Move (bps)* |
|---------|---------|-------------|--------------------------|---------------------------|
| 5 min   | 300     | 1.00        | 42.2                     | 12.7                      |
| 15 min  | 900     | 1.73        | 73.1                     | 22.0                      |
| 30 min  | 1800    | 2.45        | 103.3                    | 31.1                      |
| 60 min  | 3600    | 3.46        | 146.1                    | 43.9                      |
| Overnight| ~18000 | 7.75        | 326.8                    | 98.2                      |

*bps conversion: 1 point / 33,266 pts (median mid) * 10000 = 0.3006 bps/pt

**Validation check**: At 300s, measured mean = 42.2 pts = 12.7 bps. The R14 researcher proposal estimated "unconditional abs return ~5 bps per 5 min" for a generic index. Our measured 12.7 bps is higher, likely reflecting TMFD6's relatively high tick-level volatility. We use the measured value.

---

## 2. IC-to-Edge Conversion Formula

The standard IC-to-edge conversion for a long/short quintile strategy:

```
Expected edge per trade = IC * sigma(returns) * 2
```

Where:
- IC = rank correlation between signal and forward return
- sigma(returns) = standard deviation of returns at the horizon (approximately proportional to mean abs return / sqrt(2/pi) for normal)
- Factor 2 = long/short quintile spread (top quintile vs bottom quintile)

For a simpler long-only signal (buy when signal > 0, flat when < 0):

```
Expected edge per trade = IC * sigma(returns)
```

We use the **long-only** formula since OIDS would gate CBS entries (not a standalone long/short portfolio):

```
Expected edge per trade (bps) = IC * mean_abs_return (bps)
```

This is conservative. The mean absolute return approximates sigma * sqrt(2/pi) for a normal distribution, so using mean_abs_return directly is roughly equivalent to IC * sigma * 0.80.

---

## 3. Minimum IC Table

### Breakeven: edge = 1.33 bps (RT cost)

```
IC_min = cost / mean_abs_return
```

### 2x cost: edge = 2.66 bps

```
IC_2x = 2 * cost / mean_abs_return
```

### 3x cost: edge = 4.0 bps (target for meaningful edge after slippage)

```
IC_3x = 3 * cost / mean_abs_return
```

| Horizon | Mean Abs Return (bps) | Min IC (breakeven, 1.33 bps) | Min IC (2x cost, 2.66 bps) | Min IC (3x cost, 4.0 bps) |
|---------|----------------------|------------------------------|-----------------------------|-----------------------------|
| 5 min   | 12.7                 | 0.105                        | 0.210                       | 0.315                       |
| 15 min  | 22.0                 | 0.060                        | 0.121                       | 0.182                       |
| 30 min  | 31.1                 | 0.043                        | 0.086                       | 0.129                       |
| 60 min  | 43.9                 | 0.030                        | 0.061                       | 0.091                       |
| Overnight| 98.2                | 0.014                        | 0.027                       | 0.041                       |

### Key Takeaways

1. **5 min horizon is very hard**: Needs IC > 0.105 just to break even. The Michael et al. (2022) paper shows IC of ~0.10-0.15 on SPY overnight returns. At 5 min, even if the signal is equally strong, slippage eats the edge.

2. **15-30 min is the sweet spot**: Breakeven IC = 0.043-0.060. A signal with IC = 0.10 at 30 min gives edge = 3.1 bps, which is 2.3x cost. Viable.

3. **60 min is comfortable**: IC > 0.030 breaks even. Even a weak directional signal (IC = 0.05) gives 2.2 bps net per trade.

4. **Overnight is easiest** (IC > 0.014 for breakeven) but exposes to gap risk and violates the intraday constraint.

---

## 4. Trade Frequency and Daily PnL

### Signal Update Frequency -> Max Trades/Day

TMFD6 trading session: 08:45-13:45 = 5 hours = 300 minutes.

| Signal Frequency | Max Trades/Day | Notes |
|-----------------|---------------|-------|
| Every 5 min     | 60            | Too fast -- may overtrade |
| Every 15 min    | 20            | Reasonable |
| Every 30 min    | 10            | Conservative |
| Every 60 min    | 5             | Low frequency |

### Expected Daily PnL (NTD)

```
Daily PnL = trades_per_day * edge_per_trade_bps * contract_value / 10000
Contract value = ~300,000 NTD
```

| IC | Horizon | Edge/Trade (bps) | Trades/Day | Daily PnL (NTD) | Daily PnL (pts) |
|----|---------|-----------------|------------|-----------------|-----------------|
| 0.03 | 60 min | 1.32 (-0.01 net) | 5 | **-2** | -0.0 |
| 0.05 | 60 min | 2.20 (0.87 net) | 5 | **130** | 13 |
| 0.05 | 30 min | 1.56 (0.23 net) | 10 | **69** | 7 |
| 0.08 | 30 min | 2.49 (1.16 net) | 10 | **348** | 35 |
| 0.10 | 30 min | 3.11 (1.78 net) | 10 | **534** | 53 |
| 0.10 | 15 min | 2.20 (0.87 net) | 20 | **522** | 52 |
| 0.15 | 15 min | 3.30 (1.97 net) | 20 | **1,182** | 118 |
| 0.15 | 30 min | 4.67 (3.34 net) | 10 | **1,002** | 100 |

### Interpretation

- At IC = 0.05 (weak signal), daily PnL is 70-130 NTD. Barely covers commissions.
- At IC = 0.10 (moderate signal), daily PnL is 500-530 NTD. Marginal but real.
- At IC = 0.15 (strong signal), daily PnL is 1,000-1,200 NTD. Economically meaningful.
- **IC < 0.05 is not viable at any frequency/horizon combination.**

---

## 5. Comparison with CBS

CBS benchmark (from Round 14 OOS results):
- ~15 trades/day (rest-of-day)
- +3.00 bps/trade OOS (rest-of-day: +3.95 bps/trade)
- Daily edge: 15 * 3.00 * 300,000 / 10000 = **1,350 NTD/day** (~135 pts)
- Net of costs (1.33 bps RT): 15 * (3.00 - 1.33) * 300,000 / 10000 = **751 NTD/day** (~75 pts)

### What OIDS Needs to Match CBS

To match CBS daily PnL of ~750 NTD net:

| Horizon | Trades/Day | Needed Net Edge (bps) | Needed Gross Edge (bps) | Needed IC |
|---------|-----------|----------------------|------------------------|-----------|
| 15 min  | 20        | 1.25                 | 2.58                   | **0.117** |
| 30 min  | 10        | 2.50                 | 3.83                   | **0.123** |
| 60 min  | 5         | 5.00                 | 6.33                   | **0.144** |

**OIDS needs IC > 0.12 at 15-30 min horizons to match CBS.** This is a high bar -- roughly equivalent to the best result reported by Michael et al. (2022) on SPY.

### Realistic Scenario: OIDS as CBS Overlay

If OIDS gates CBS (only enter when options signal is favorable), it doesn't need to match CBS outright. Instead:
- CBS fires ~15 times/day
- OIDS filter keeps only the top 50% of entries (8 trades/day)
- If filtered entries have +5.0 bps/trade vs unfiltered +3.0 bps/trade
- Daily PnL = 8 * (5.0 - 1.33) * 30 = **882 NTD/day** (better than unfiltered CBS)

This requires the OIDS signal to be able to rank CBS entries by quality. Even a modest IC (~0.05-0.08) could improve CBS hit rate if the signal is orthogonal.

---

## 6. Kill Gate Recommendation

### The Challenger Proposed IC < 0.02

Let me evaluate this threshold against the cost model:

| IC = 0.02 | Horizon | Edge/Trade (bps) | Net Edge (bps) | Viable? |
|-----------|---------|-----------------|----------------|---------|
| 0.02      | 5 min   | 0.25            | -1.08          | NO      |
| 0.02      | 15 min  | 0.44            | -0.89          | NO      |
| 0.02      | 30 min  | 0.62            | -0.71          | NO      |
| 0.02      | 60 min  | 0.88            | -0.45          | NO      |

**IC = 0.02 is deeply unprofitable at every horizon.** The Challenger's proposed kill gate is too generous.

### Recommended Kill Gate: IC < 0.05

Rationale:
1. IC = 0.05 at 60 min gives net edge = 0.87 bps -- barely viable as standalone, but potentially useful as CBS filter.
2. IC = 0.05 at 30 min gives net edge = 0.23 bps -- not standalone viable, but signal ranking could help CBS.
3. IC < 0.05 means the signal is too weak to improve CBS even as a filter (ranking quality too low to separate good from bad CBS entries).
4. Margin of safety: at IC = 0.05, daily PnL is 70-130 NTD -- barely covers execution costs. Below this, OIDS adds negative value.

### Kill Gate Decision Table

| IC Measured (30 min forward return) | Decision |
|-------------------------------------|----------|
| IC < 0.05                           | **KILL** -- signal too weak for any use case |
| IC = 0.05-0.08                      | **CONDITIONAL** -- only viable as CBS filter, not standalone. Proceed only if CBS overlay backtests show improvement. |
| IC = 0.08-0.12                      | **PROCEED** -- viable standalone at 30-60 min. Build live pipeline. |
| IC > 0.12                           | **STRONG PROCEED** -- can match CBS economics. Priority implementation. |

### Additional Kill Conditions (regardless of IC)

1. **Statistical significance**: IC must have p < 0.10 with non-overlapping windows (minimum 100 independent observations). The Stage 2 prototype MUST use non-overlapping sampling -- the Round 17 TSMC lead-lag prototype was rejected precisely because overlapping windows inflated significance (N=31K vs N_eff=52).
2. **Minimum data coverage**: TXO data must overlap with TMFD6 for at least 20 trading days. Fewer than 20 days produces unreliable IC estimates (see R17 Stage 2 challenger review: 3 days was insufficient).
3. **IV computability**: If > 30% of TXO ticks have insufficient data for IV computation (missing underlying price, illiquid strikes), the signal surface is too thin. Kill.

---

## Summary

| Question | Answer |
|----------|--------|
| Min IC for breakeven? | 0.030 (60 min), 0.043 (30 min), 0.060 (15 min), 0.105 (5 min) |
| Min IC for 2x cost? | 0.061 (60 min), 0.086 (30 min), 0.121 (15 min) |
| IC needed to match CBS? | ~0.12 at 15-30 min horizon |
| Recommended kill gate? | **IC < 0.05** (stricter than Challenger's 0.02) |
| Best horizon for OIDS? | 30-60 min (breakeven IC is lowest, trade frequency still reasonable) |
| Daily PnL at IC=0.10, 30 min? | ~534 NTD (~53 pts) |
| OIDS as CBS overlay potential? | YES -- even IC=0.05-0.08 could improve CBS hit rate if orthogonal |

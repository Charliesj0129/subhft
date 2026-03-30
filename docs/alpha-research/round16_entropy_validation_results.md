# Round 16: Order-Flow Entropy Quintile Validation Results

**Date**: 2026-03-26
**Validator**: Execution Agent
**Data**: TMFD6 L1 tick data, 4 days (2 tight-spread March, 2 wide-spread Jan/Feb)

---

## Methodology

1. **Trade direction inference**: Tick rule on mid-price changes (up=buy, down=sell, unchanged=carry previous direction)
2. **1-second aggregation**: Majority vote of tick-level directions per second
3. **3-state Markov model**: States = {sell=0, neutral=1, buy=2}
4. **30-second rolling conditional Shannon entropy** of the 3x3 transition matrix
5. **Quintile split** by entropy value
6. **Forward absolute returns** at 60-second and 300-second horizons

Pass threshold: Q1 (lowest entropy) / Q5 (highest entropy) absolute return ratio > 2.0

---

## Results

### March 19 (tight spread, 3 pts median)

| Quintile | N | Entropy Range | Mean |60s ret| | Mean |300s ret| |
|----------|---|--------------|----------------|-----------------|
| Q1 (low) | 9,077 | [0.441, 0.988] | 16.02 pts | 37.29 pts |
| Q2 | 8,983 | [0.988, 1.075] | 16.89 pts | 41.70 pts |
| Q3 | 9,142 | [1.075, 1.137] | 18.13 pts | 41.81 pts |
| Q4 | 9,096 | [1.137, 1.234] | 17.25 pts | 40.07 pts |
| Q5 (high) | 9,160 | [1.234, 1.549] | 17.71 pts | 38.19 pts |

**Q1/Q5 ratio (60s): 0.904 -- FAIL**
**Q1/Q5 ratio (300s): 0.977 -- FAIL**

No monotonic relationship. Returns are essentially FLAT across entropy quintiles.

### March 20 (tight spread)

**Q1/Q5 ratio (60s): 1.060 -- FAIL**
**Q1/Q5 ratio (300s): 0.974 -- FAIL**

Same pattern: no relationship between entropy and forward absolute return magnitude.

### January 30 (wide spread, 28 pts median)

| Quintile | N | Entropy Range | Mean |60s ret| | Mean |300s ret| |
|----------|---|--------------|----------------|-----------------|
| Q1-Q2 | 0 | [0.000, 0.000] | N/A | N/A |
| Q3 | 38,748 | [0.000, 0.179] | 8.57 pts | 23.11 pts |
| Q4 | 13,132 | [0.179, 0.331] | 11.17 pts | 26.31 pts |
| Q5 (high) | 13,455 | [0.331, 1.030] | 13.25 pts | 30.12 pts |

**Q1/Q5 ratio: N/A (Q1-Q2 are EMPTY -- median entropy is 0.000)**

The wide-spread regime produces degenerate entropy: 59% of seconds have zero entropy (the transition matrix is dominated by neutral->neutral transitions because mid-price changes occur on only 1.1% of ticks). The entropy signal is meaningless in this regime.

Furthermore, the direction is OPPOSITE to the hypothesis: higher entropy -> LARGER returns (Q5 has the highest absolute returns). This is the reverse of Singha's finding on SPY.

### February 4 (wide spread)

Same degenerate pattern as Jan 30. Median entropy = 0.000. Q1-Q2 empty.

---

## Verdict: FAIL

**Order-flow entropy does NOT predict move magnitude on TMFD6.**

| Day | Q1/Q5 (60s) | Q1/Q5 (300s) | Result |
|-----|-------------|-------------|--------|
| Mar 19 | 0.904 | 0.977 | FAIL |
| Mar 20 | 1.060 | 0.974 | FAIL |
| Jan 30 | N/A (degenerate) | N/A | FAIL |
| Feb 04 | N/A (degenerate) | N/A | FAIL |

All four days FAIL the Q1/Q5 > 2.0 threshold. No day is even close.

### Root Causes

1. **Trade direction inference from L1 quotes is too lossy**: The paper uses actual SPY trade records with 100% classification. Our tick-rule proxy from mid-price changes classifies only 49% of ticks in tight-spread regime and 1.1% in wide-spread regime. The resulting Markov chain is dominated by the "neutral" state, collapsing entropy variation.

2. **3-state model is too coarse**: The paper uses a 15-state model incorporating trade size, which captures much richer flow patterns. Our 3-state (buy/sell/neutral) model cannot distinguish informed from uninformed flow.

3. **1-second resolution may not match TMFD6 microstructure**: SPY has ~10,000 ticks/minute; TMFD6 has ~108 (tight) to ~18 (wide). At 1-second resolution, most TMFD6 seconds have 2-5 ticks, producing very noisy state estimates.

4. **Wide-spread regime is completely degenerate**: Zero entropy for 59% of observations means the signal has no discriminative power.

### Recommendation

**Close this direction.** Even with proper trade records from ClickHouse, the fundamental mismatch between SPY's ultra-high-frequency microstructure and TMFD6's thin order book makes this approach unlikely to transfer. The paper's result depends on information density that does not exist on TMFD6.

If trade records become available and the team wants a second attempt, use the full 15-state model with trade size buckets. But the prior from this test is strongly negative.

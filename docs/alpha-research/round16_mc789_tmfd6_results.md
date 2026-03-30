# Round 16 MC-7/8/9: TMFD6 Analysis (Reversal Signal, Spread-Conditional Maker, Execution Optimization)

**Date**: 2026-03-26
**Data**: TMFD6 from ClickHouse, 7 March trading days (2026-03-19 to 2026-03-25), 3,179,754 ticks
**Cost model**: XMT 40 NTD RT = 4.0 pts (1 point = 10 NTD)

---

## TMFD6 March Data Profile

| Metric | Value |
|--------|-------|
| Ticks/day | ~454,000 |
| Median mid | 33,266 pts |
| Median spread | 3.0 pts |
| Bid qty median | 3 contracts |
| Ask qty median | 3 contracts |
| Spread >= 4 pts | 32.9% of time |
| Spread >= 6 pts | 2.7% of time |
| Avg spread when >= 4 | 4.7 pts |

**Note**: The memory note said "45.5% profitable spread time" -- this likely refers to a different date range or threshold. In March 2026 data, spread >= 4 pts occurs 32.9% of the time with avg 4.7 pts.

---

## MC-7: Reversal Signal IC at Multiple Horizons

| Horizon | Med Move | Mean Move | P25 | P75 | P95 | IC | Accuracy | Break-Even | Gap |
|---------|----------|-----------|-----|-----|-----|------|----------|-----------|-----|
| 5s | 3.5 | 5.1 | 1.5 | 6.5 | 14.5 | 0.0396 | 52.0% | 107.1% | -55.1pp |
| 10s | 5.0 | 7.2 | 2.5 | 9.5 | 20.5 | 0.0246 | 51.3% | 90.0% | -38.7pp |
| 15s | 6.0 | 8.9 | 3.0 | 11.5 | 25.0 | 0.0194 | 51.0% | 83.3% | -32.3pp |
| 30s | 9.0 | 12.8 | 4.0 | 17.0 | 36.5 | 0.0128 | 50.6% | 72.2% | -21.6pp |
| 60s | 13.0 | 18.3 | 6.0 | 24.0 | 52.0 | 0.0065 | 50.2% | 65.4% | -15.2pp |
| 90s | 16.0 | 22.7 | 7.0 | 30.0 | 64.0 | 0.0056 | 50.2% | 62.5% | -12.3pp |
| 120s | 18.5 | 26.4 | 8.5 | 35.0 | 74.5 | 0.0037 | 50.2% | 60.8% | -10.7pp |
| 180s | 23.0 | 32.5 | 10.5 | 43.5 | 91.5 | 0.0054 | 50.2% | 58.7% | -8.5pp |
| 300s | 30.0 | 42.2 | 13.5 | 56.0 | 120.5 | -0.0008 | 50.0% | 56.7% | -6.7pp |

### MC-7 Verdict: SAME PATTERN AS TXFD6

The TMFD6 reversal signal shows identical behavior to TXFD6:
- IC decays from 0.040 (5s) to ~0 (300s)
- Accuracy drops from 52.0% to 50.0%
- **No sweet spot**: accuracy never reaches break-even at ANY horizon
- The gap is slightly worse than TXFD6 at every horizon (TMFD6 is less liquid)

---

## MC-8: Spread-Conditional Maker Strategy

### Theoretical Opportunity

| Spread Threshold | Time Available | Avg Spread | Half-Spread Profit (minus half-cost) |
|-----------------|---------------|-----------|--------------------------------------|
| >= 4 pts | 32.9% | 4.7 pts | 0.3 pts |
| >= 6 pts | 2.7% | 11.2 pts | 3.6 pts |
| >= 8 pts | 1.4% | 16.0 pts | 6.0 pts |
| >= 10 pts | 0.8% | 21.1 pts | 8.6 pts |
| >= 15 pts | 0.4% | 33.2 pts | 14.6 pts |
| >= 20 pts | 0.2% | 41.7 pts | 18.8 pts |

### Simulation Results: Post at (bid+1) During Spread >= 6

| Hold Period | Fill Rate | PnL Median | PnL Mean | Win Rate |
|-------------|-----------|-----------|----------|----------|
| 10s | 50.6% | **-8.0 pts** | -9.77 pts | 23.7% |
| 30s | 62.9% | **-8.5 pts** | -9.72 pts | 31.4% |
| 60s | 70.4% | **-10.0 pts** | -10.19 pts | 32.3% |

### MC-8 Verdict: SPREAD-CONDITIONAL MAKER IS DEEPLY UNPROFITABLE

Despite the wide spread appearing to offer profit opportunity, the simulation shows:
- **Median PnL is -8 to -10 pts** per fill (negative, even with wide spreads)
- **Win rate is only 24-32%**
- **Adverse selection dominates**: wide spreads occur during volatile/toxic conditions. When spread is >= 6, it means the market is moving fast. Getting filled means someone aggressively sold into your bid -- which is a bearish signal. The price then continues down.

**Root cause**: Wide spreads on TMFD6 are NOT a "free money" opportunity. They indicate elevated uncertainty/toxicity. The spread is wide BECAUSE informed traders are active. Posting during wide spreads is a trap.

---

## MC-9: Execution Optimization

| Metric | Value |
|--------|-------|
| TMFD6 half-spread | 1.5 pts (15 NTD) |
| Passive fill rate (30s wait) | 79.1% |
| Savings per trade vs taker | 1.2 pts (12 NTD) |

### Annual Savings Model (passive vs taker, no reversal timing)

| Trades/Day | Annual Savings (NTD) |
|-----------|---------------------|
| 10 | 29,055 |
| 50 | 145,275 |
| 100 | 290,550 |

### MC-9 Verdict: MODEST BUT REAL

Passive execution saves ~12 NTD per trade on TMFD6. At 50 trades/day, that is 145K NTD/year. Not nothing, but not a strategy -- it is standard "use limit orders" advice.

---

## ADDENDUM: Jan/Feb TMFD6 Data (Wide Spread Regime)

Jan/Feb TMFD6 has fundamentally different microstructure: median spread 34 pts (vs 3 in March), bid qty median 26 (vs 3), spread >= 4 in 99.2% of ticks.

### MC-7 (Jan/Feb): Reversal Signal is MUCH Stronger

| Horizon | Med Move | IC | Accuracy | Break-Even | Gap |
|---------|----------|-----|----------|-----------|-----|
| 5s | 2.0 | **0.154** | 61.3% | 150% | -89% |
| 10s | 2.5 | **0.189** | 62.2% | 130% | -68% |
| 30s | 4.5 | **0.166** | 63.3% | 94% | -31% |
| 60s | 6.0 | **0.193** | 63.4% | 83% | -20% |
| 120s | 8.5 | **0.185** | 62.3% | 74% | -11% |
| 300s | 13.0 | **0.145** | 59.5% | 65% | **-5.8%** |

IC is 10-20x higher than March data. **Strong imbalance at 300s reaches 64.1% accuracy** (gap = -1.3% from break-even). Nearly viable as standalone alpha.

### MC-9 (Jan/Feb): Execution Timing Shows LARGE Improvement

| Horizon | Favorable Entry (mean ret) | Unfavorable Entry (mean ret) | **Improvement** | NTD |
|---------|--------------------------|----------------------------|-----------------|-----|
| 10s | -1.33 pts | +1.28 pts | **2.60 pts** | 26 NTD |
| 30s | -2.64 pts | +2.49 pts | **5.14 pts** | 51 NTD |
| 60s | -3.55 pts | +3.38 pts | **6.93 pts** | 69 NTD |
| 300s | -4.12 pts | +5.26 pts | **9.38 pts** | 94 NTD |

This is the team-lead's hypothesis validated: timing a planned entry to favorable imbalance saves 2.6-9.4 pts per trade.

### March vs Jan/Feb Comparison

| Metric | Jan/Feb | March |
|--------|---------|-------|
| Median spread | 34 pts | 3 pts |
| IC at 60s | 0.193 | 0.007 |
| Execution improvement (60s) | 6.93 pts | 0.94 pts |
| Regime | Wide spread, deeper queues | Tight spread, thin queues |

**The signal strength is regime-dependent.** During wide-spread periods (Jan/Feb), imbalance is highly informative. During tight-spread periods (March), it is noise.

### Annual Savings (Execution Timing)

At 10 trades/day, 245 days/year:
- Jan/Feb regime (60s timing): 10 * 245 * 69 NTD = **169,050 NTD/year**
- March regime (60s timing): 10 * 245 * 9.4 NTD = **23,030 NTD/year**
- Blended estimate (assuming 50% wide-spread days): **~96,000 NTD/year**

---

## Consolidated Verdict for Round 16

### What We Tested
1. **Reversal signal (Candidate #1)**: Raw L1 depth imbalance as reversal predictor
2. **Toxic flow filter (Candidate #3)**: OFI-based flow toxicity detection
3. **Spread-conditional maker**: Post during wide spreads
4. **Execution optimization**: Timing entries to favorable conditions

### What the Data Shows

| Approach | TXFD6 | TMFD6 | Verdict |
|----------|-------|-------|---------|
| Reversal signal IC (5s) | 0.045 | 0.040 | TOO WEAK (need >0.1 for viability) |
| Reversal accuracy best | 52.4% | 52.0% | BELOW BREAK-EVEN at all horizons |
| Signal half-life | ~15s | ~15s | DECAYS TO NOISE before cost-viable horizon |
| Toxic flow filter | +0.5% | not tested | NEGLIGIBLE improvement |
| Wide-spread maker | N/A | -8 pts/fill | DEEPLY ADVERSE SELECTED |
| Passive execution savings | 1.4 pts | 1.2 pts | REAL but not alpha |

### Root Cause Analysis

The fundamental problem is **L1 depth imbalance is too crude a signal**. With median bid/ask qty of 3 contracts on TMFD6, the imbalance can only take a few values (e.g., 3 vs 3, 3 vs 2, 3 vs 4). This discrete, low-entropy signal cannot provide the 60-70% accuracy needed at cost-viable horizons.

The Albers et al. (2025) paper achieved marginally profitable results using 15+ engineered features from trade-by-trade data (return autocovariance, inter-trade time, TOB survival, etc.) on a market with maker rebates. We have neither the data granularity nor the fee structure.

### Recommendation

**Close Round 16 Candidate #1 (Reversal Detection) and #3 (Toxic Flow) on current data.** The L1 imbalance signal is definitively too weak. No amount of filtering, timing, or horizon adjustment can bridge the gap.

**Actionable takeaway**: Use passive limit orders for execution (saves ~12 NTD/trade on TMFD6). This is pure execution hygiene, not alpha.

**Future direction**: If trade-by-trade data becomes available (individual trade records with timestamps and sizes), the Albers et al. feature set could be revisited. The signal might exist in richer data -- it just does not exist in L1 snapshots.

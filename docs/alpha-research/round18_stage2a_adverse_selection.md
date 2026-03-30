# R18 Stage 2a: TMFD6 Adverse Selection Rate Measurement

**Date**: 2026-03-26
**Analyst**: Researcher Agent
**Data**: ClickHouse `hft.market_data`, TMFD6, March 19-26 2026 (8 trading days)
**Events**: 3,355,409 BidAsk + 1,391,056 Tick = 4,746,465 total

---

## CRITICAL FINDING: Spread Regime Has Changed

**The 45.5% "profitable spread time" assumption from the Stage 1 survey is INVALID for the current market regime.**

| Period | Median Spread | Spread >= 5 (time pct) | Regime |
|--------|--------------|----------------------|--------|
| Jan 2026 | 30-56 pts | ~100% | Very illiquid (early subscription, few participants) |
| Feb 2026 | 5-53 pts | 75-100% | Transitional |
| Mar 2026 | **3 pts** | **0.6-12.3%** | Current: liquid, tight spread |

The earlier 45.5% figure was computed over the Jan-Mar combined dataset, dominated by the illiquid Jan-Feb period when TMFD6 had median spreads of 20-50+ pts. **The current regime (March 2026) has median spread of 3 pts, with spread >= 5 occurring only ~4.8% of BidAsk time.**

This fundamentally changes the R18 strategy economics.

---

## Table 1: Adverse Selection Rate by Spread Bucket (March 2026)

Data: 1,391,045 valid trades. Adverse = mid-price moved in taker's direction (against maker).
AvgChg = average mid-price change in maker-adverse direction (positive = bad for maker).

| Spread Bucket | N Trades | Adv Rate (1s) | AvgChg (1s) | Adv Rate (5s) | AvgChg (5s) | Adv Rate (30s) | AvgChg (30s) |
|--------------|---------|--------------|------------|--------------|------------|---------------|-------------|
| 1-3 | 867,136 | 48.9% | +0.38 pt | 50.0% | +0.36 pt | 50.1% | +0.33 pt |
| 4 | 408,531 | 48.5% | +0.34 pt | 49.7% | +0.39 pt | 49.8% | +0.40 pt |
| **5-6** | **67,323** | **50.9%** | **+0.65 pt** | **51.1%** | **+0.92 pt** | **50.3%** | **+1.20 pt** |
| **7-10** | **30,846** | **51.0%** | **+0.84 pt** | **51.1%** | **+1.59 pt** | **51.8%** | **+3.48 pt** |
| **11-20** | **10,969** | **50.5%** | **+1.82 pt** | **51.7%** | **+3.03 pt** | **51.3%** | **+5.21 pt** |
| **20+** | **6,914** | **52.2%** | **+3.57 pt** | **45.1%** | **+1.62 pt** | **50.4%** | **+2.75 pt** |
| **>=5 ALL** | **115,378** | **51.0%** | **+0.97 pt** | **50.9%** | **+1.32 pt** | **50.8%** | **+2.31 pt** |

### Interpretation

- Adverse selection rate is remarkably close to 50% across all buckets and horizons
- At wide spreads (>= 5), the rate is 50.9% at 5s -- essentially a coin flip
- The average adverse mid-change at 5s is +1.32 pts for wide-spread trades
- At spread 20+, the 5s adverse rate actually drops to 45.1% (favorable for makers!)

---

## Table 2: Adverse Selection by Time-of-Day (Spread >= 5 only)

| Time Window | N Trades | Adv Rate (1s) | AvgChg (1s) | Adv Rate (5s) | AvgChg (5s) | Adv Rate (30s) | AvgChg (30s) |
|------------|---------|--------------|------------|--------------|------------|---------------|-------------|
| Open 08:45-09:15 | 44,711 | 51.4% | +0.61 pt | 51.2% | +0.48 pt | 51.6% | +1.53 pt |
| Morning 09:15-10:00 | 5,812 | 54.0% | +0.79 pt | 50.1% | +0.01 pt | 48.7% | +0.56 pt |
| Midday 10:00-12:00 | 2,765 | 51.1% | +0.44 pt | 53.2% | +0.60 pt | 49.0% | +0.26 pt |
| Afternoon 12:00-13:00 | 146 | 35.6% | -0.28 pt | 53.4% | +0.78 pt | 39.7% | -2.26 pt |
| Close 13:00-13:45 | 4,221 | 46.1% | +0.23 pt | 47.7% | -0.03 pt | 48.4% | +0.09 pt |

### Interpretation

- **Opening (08:45-09:15)** concentrates 39% of wide-spread trades (44,711 of 115,378). Wide spreads cluster at open.
- **Close (13:00-13:45)** shows slight maker-favorable adverse rate (47.7% at 5s, near-zero adverse change)
- **Afternoon (12:00-13:00)** has almost no wide-spread trades (146) -- not meaningful
- No time window shows dramatically elevated adverse selection

---

## Table 3: Spread-Volatility Correlation

| Metric | Value |
|--------|-------|
| 1-min spread vs volatility correlation | **+0.30** |

| Spread Bucket | Mean 1-min Volatility (pts) | N Minutes |
|--------------|---------------------------|-----------|
| 1-3 | 1.31 | 3,287 |
| 5-6 | 1.52 | 34 |
| 7-10 | 6.76 | 28 |
| 11-20 | 6.65 | 28 |
| 20+ | 10.85 | 20 |

**Interpretation**: Wider spreads do correlate with higher volatility (r=0.30). Wide-spread periods see 5-8x higher volatility than tight-spread periods. This means wide-spread periods are partly information-driven (not purely structural), but the adverse selection rate remains near 50% -- the volatility does NOT disproportionately hurt makers.

---

## Kill Gate Evaluation

### GATE 1: Adverse rate at spread >= 5 (5s horizon)

**Result: 50.9% -- PASS (threshold: 60%)**

The adverse selection rate is essentially 50% (coin flip). This means that on average, a maker who posts at the touch during wide-spread periods has no systematic directional disadvantage.

### GATE 2: Adverse rate monotonicity with spread

| Bucket | Adv Rate (5s) |
|--------|--------------|
| 5-6 | 51.1% |
| 7-10 | 51.1% |
| 11-20 | 51.7% |
| 20+ | 45.1% |

**Result: NOT monotonically increasing -- PASS**

Adverse selection does NOT increase with spread width. In fact, the widest spreads (20+) show the LOWEST adverse rate (45.1%), suggesting these are structural liquidity gaps rather than informed-flow-driven events.

### GATE 3 (NEW): Spread regime viability

**Result: CAUTION**

The current regime has spread >= 5 only 4.8% of BidAsk time (not 45.5% as assumed in Stage 1). This dramatically reduces the opportunity set for Direction B (Spread-Gated Selective LP).

---

## Maker Edge Estimate (Spread >= 5 regime)

| Component | Value |
|-----------|-------|
| Average half-spread capture | +4.50 pts |
| Average adverse selection (5s) | -1.32 pts |
| Fee per leg | -2.00 pts |
| **Net edge per leg** | **+1.18 pts** |
| **Net edge per roundtrip** | **+2.37 pts (23.7 NTD)** |

This is a positive edge estimate, but it assumes:
1. Fill at the touch (no queue-position disadvantage)
2. Both legs fill (no one-sided exposure)
3. No execution latency cost (36ms RTT not modeled)
4. 4.8% opportunity time -- approximately 14 minutes per 5-hour session

---

## Revised Strategic Implications

### Direction B (SG-LP) viability downgraded

The spread-gating strategy assumed 45.5% eligible time. The actual March 2026 figure is **4.8%**. This means:
- ~14 minutes of eligible quoting per 300-minute session
- At 1.8 ticks/sec and maybe 50% fill rate: ~750 potential fills per day at wide spread
- At +2.37 pts/roundtrip: ~375 roundtrips x 23.7 NTD = ~8,900 NTD/day theoretical max
- After execution frictions, realistic expectation: 2,000-5,000 NTD/day at best

This is not zero, but it's a thin opportunity that could disappear as TMFD6 liquidity continues to improve.

### Direction A (RCM) may apply at TIGHT spreads too

The interesting finding is that adverse selection at spread = 4 (median) is 49.7% -- essentially 50%. This means a reversal-conditional maker strategy might work even at tight spreads, where volume is 8x higher. The challenge is that at spread = 4, the half-spread capture (2 pts) minus fees (2 pts) = 0 pts gross edge. The only edge would come from picking favorable fills via the reversal signal.

### Direction C (IBH) needs recalibration

The A-S framework parameters need to be recalibrated for the current tight-spread regime, not the historical wide-spread regime.

---

## Raw Data Summary

```
Total BidAsk events (March):  3,355,409
Total Tick events (March):    1,391,056
Spread >= 5 BidAsk pct:      4.8%
Spread >= 5 Tick pct:         8.3%
Average spread (all):         3.0 pts
Average spread (>= 5):       9.0 pts
```

**Analysis script**: `research/experiments/validations/tmfd6_adverse_selection.py`
**Data source**: ClickHouse export `/tmp/tmfd6_march.tsv`

# BLOCKER-E2: TMFD6 Fill Rate at Wide Spreads

**Date**: 2026-03-26
**Author**: Challenger (R18 team)
**Script**: `research/experiments/validations/tmfd6_fill_rate/measure_fill_rate.py`
**Data**: `research/data/raw/tmfd6/TMFD6_all_l1.npy` (7.75M rows, 24 trading days)

---

## Executive Summary

**Overall fill rate: 1.8%** (322 fills out of 17,632 wide-spread episodes).
Average 13.4 fills/session. Winner's curse at 5s: 45.3% adverse, avg adverse magnitude 4.90 pts.

**Kill gate: PASS** (fills/session >= 5, winner's curse < majority adverse at net spread).
**Practical assessment: MARGINAL** -- fill rate is very low, most spread-capture value concentrates in a few high-activity days.

---

## Methodology

### Simulation Design
- **Wide-spread episode**: Contiguous period where `ask_px - bid_px >= 5 pts`
- **Simulated action**: Post 1-lot at the touch (shorter-queue side) when episode starts
- **Queue tracking**: L1 quote-only data; infer fills from queue depletion (same price, qty decrease)
- **Order lifecycle**: Cancel if spread narrows < 5 or price moves away

### Simplification Assumptions
1. **FIFO queue priority** -- realistic for TAIFEX
2. **36ms latency** before order enters queue (measured Shioaji broker RTT)
3. **One side per episode** -- post on the side with shorter queue
4. **1-lot fills only** -- no partial fill tracking
5. **Queue depletion = fill** -- L1 qty decrease at same price treated as queue consumption. This **overstates** fill rate because some decreases are cancellations, not fills. Conservative for a kill gate (real fill rate is lower).
6. **No queue improvement** -- we always join the back; no price improvement or re-posting

---

## Results

### Overall Fill Rate

| Metric | Value |
|--------|-------|
| Total wide-spread episodes | 17,632 |
| Filled | 322 |
| Not filled | 17,310 |
| **Overall fill rate** | **1.8%** |

### Fill Rate by Spread Bucket (points)

| Spread | Episodes | Filled | Rate | Avg Duration |
|--------|----------|--------|------|-------------|
| [5-9] | 16,868 | 311 | 1.8% | 40.5s |
| [10-19] | 609 | 10 | 1.6% | 2,438.8s |
| [20-39] | 113 | 0 | 0.0% | 2,408.0s |
| [40+] | 42 | 1 | 2.4% | 8,670.0s |

**Key finding**: 95.7% of episodes are in the [5-9] bucket. Wider spreads (10+) are longer-lived but paradoxically harder to fill -- queue depth is deeper and price is stale (no one is trading).

### Time-to-Fill (filled episodes only)

| Stat | Value |
|------|-------|
| Median | 1.51s |
| Mean | 3.57s |
| P25 | 0.72s |
| P75 | 3.63s |
| P95 | 13.47s |

Fills that happen are fast (median 1.5s). This suggests fills occur when the market briefly touches our price, not through sustained queue drainage.

### Fills per Trading Day

| Date | Episodes | Fills | Rate |
|------|----------|-------|------|
| 01/27 | 27 | 0 | 0.0% |
| 01/28 | 37 | 1 | 2.7% |
| 01/29 | 40 | 0 | 0.0% |
| 01/30 | 27 | 0 | 0.0% |
| 01/31 | 15 | 1 | 6.7% |
| 02/03 | 16 | 1 | 6.2% |
| 02/04 | 43 | 0 | 0.0% |
| 02/05 | 16 | 0 | 0.0% |
| 02/06 | 58 | 2 | 3.4% |
| 02/07 | 32 | 0 | 0.0% |
| 02/10 | 15 | 1 | 6.7% |
| 02/11 | 4 | 0 | 0.0% |
| 02/23 | 1,296 | 39 | 3.0% |
| 02/24 | 2,814 | 107 | 3.8% |
| 02/25 | 2,921 | 84 | 2.9% |
| 02/26 | 551 | 11 | 2.0% |
| 03/19 | 148 | 1 | 0.7% |
| 03/20 | 839 | 15 | 1.8% |
| 03/21 | 89 | 1 | 1.1% |
| 03/23 | 5,351 | 16 | 0.3% |
| 03/24 | 1,745 | 19 | 1.1% |
| 03/25 | 1,197 | 18 | 1.5% |
| 03/26 | 351 | 5 | 1.5% |

**Total fills**: 322 across 24 days = **13.4 fills/session**

**Distribution is extremely skewed**: Feb 23-25 alone account for 230/322 fills (71.4%). Many days have 0-2 fills. The "average 13.4/day" is misleading -- median day has ~1 fill.

### Queue Depth at Fill

| Stat | Value |
|------|-------|
| Mean initial queue | 1.4 lots |
| Median initial queue | 1 lot |
| P75 | 1 lot |

Fills overwhelmingly happen at queue depth = 1. This means we only fill when we are essentially alone at the touch and someone takes immediately. Deep queue positions never fill.

---

## Winner's Curse Analysis

### Post-Fill Mid-Price Movement

| Horizon | Total | Adverse % | Avg Signed Move (pts) | Avg Adverse Magnitude (pts) |
|---------|-------|-----------|----------------------|---------------------------|
| 1s | 322 | 38.5% | +0.21 | 2.27 |
| 5s | 322 | 45.3% | -0.39 | 4.90 |
| 30s | 322 | 51.6% | -0.14 | 10.89 |

### Spread Capture vs Adverse Selection

- **Average spread at fill**: 6.2 pts
- **Half-spread capture**: 3.1 pts (maker captures half spread)
- **Half RT cost**: 2.0 pts (one leg of 4 pt RT cost)
- **Net spread capture**: 1.1 pts per fill
- **Average adverse at 5s**: 4.90 pts (when adverse)
- **Expected adverse cost**: 45.3% x 4.90 = 2.22 pts

**Net expected P&L per fill**: 1.1 - 0.39 = **+0.71 pts** (using avg signed movement at 5s)

This is marginally positive at 5s horizon but the 30s adverse rate crosses 50% -- holding longer erases the edge.

---

## Kill Gate Assessment

| Gate | Threshold | Measured | Result |
|------|-----------|----------|--------|
| Fills/session | >= 5 | 13.4 | **PASS** |
| Winner's curse > net spread (majority) | adverse% > 50% AND avg adverse > net capture | 45.3%, not majority | **PASS** |

**Overall: PASS** -- but barely.

---

## Critical Caveats

1. **Fill rate is overstated**: L1 queue depletion includes cancellations, not just fills. Real fill rate is likely **< 1%**.

2. **Fill clustering**: 71% of fills come from 3 days (Feb 23-25). Most days produce 0-2 fills. A strategy depending on these fills would have enormous variance.

3. **Queue depth = 1**: We only fill when essentially alone at the touch. This means:
   - We are the marginal liquidity provider
   - When someone hits us, it is likely informed flow
   - The 38.5% adverse at 1s and 45.3% at 5s confirms moderate winner's curse

4. **Spread capture is thin**: Net 1.1 pts per fill x 13.4 fills/day = 14.7 pts/day. At 10 NTD/pt, that is 147 NTD/day (~$4.50 USD). This is not a viable strategy on its own.

5. **Regime dependence**: The Feb 23-25 cluster likely corresponds to a specific market microstructure regime (e.g., contract rollover, volatility event). Strategy viability depends on these regimes recurring.

---

## Recommendation

**PASS kill gate, but MARGINAL viability as standalone strategy.**

Fill rate of 1.8% (overstated) with 13.4 fills/day (skewed distribution) and 1.1 pts net capture per fill produces negligible expected revenue. The data passes the formal kill gates but the practical economics are very thin.

**For the R18 team**: This fill rate measurement should be used as a constraint input, not a green light. Any strategy requiring passive fills at wide spreads must account for:
- Real fill rate likely < 1%
- Most days will produce 0-2 fills
- Winner's curse erodes ~40% of gross spread capture at 5s
- Revenue per day is order-of-magnitude ~150 NTD ($4.50)

# R18 Stage 2a: TMFD6 Fill Rate at Wide Spreads (BLOCKER-E2)

**Date**: 2026-03-26
**Analyst**: Claude (Challenger Agent)
**Data**: `TMFD6_all_l1.npy` -- 7,747,814 L1 snapshots, 20 trading days (2026-01-26 to 2026-03-26)
**Status**: Complete

---

## CRITICAL FINDING: Contract Roll Regime Collapse

**Before evaluating fill rates, a structural data issue invalidates R18's core premise.**

The survey's claim that TMFD6 has "45.5% profitable spread time (spread >= 5 pts)" is an artifact of mixing far-month and front-month contract data. The spread regime collapses when TMFD6 becomes the front-month contract:

| Period | Contract Status | Median Spread | Spread >= 5 | Spread >= 10 | Avg Spread (when >= 5) |
|--------|----------------|--------------|-------------|--------------|----------------------|
| **Jan-Feb** | Far month | **29 pts** | **96.4%** | 79.4% | 35.8 pts |
| **March** | Front month | **3 pts** | **6.15%** | 0.98% | 7.7 pts |
| **Combined (reported)** | Mixed | 7 pts | 57.8% | -- | ~19.7 pts |

**Implication**: The "45.5% profitable spread" that motivates all three R18 directions is inflated by far-month data. In the front-month regime (when the contract is actually liquid and tradeable), only **6.15%** of time has spread >= 5 pts. This is structurally similar to TXFD6's 2.1% -- the exact condition R16 identified as an "adverse selection trap."

This finding may need independent verification on TMFC6/TMFB6 contract data to confirm the pattern is contract-roll-driven rather than a January-specific anomaly.

---

## Fill Rate Simulation Results

### Methodology

- Simulated 1-lot passive orders at L1 touch during spread >= 5 periods
- Join back of queue at current L1 depth
- Repost every 60 seconds during sustained wide-spread periods
- Post on both sides (bid and ask) simultaneously
- Cancel conditions: price moves away, spread narrows below 5, or 120s timeout
- Track queue consumption from L1 quantity decreases at same price level

### Overall Results

| Metric | Value |
|--------|-------|
| Total simulated posts | 26,026 |
| Total fills | 7,688 |
| Total cancels | 18,338 |
| **Overall fill rate** | **29.5%** |
| **Fills per session (all days)** | **384.4** |
| Fills per session (March only) | **87.0** |

**Kill gate (fills/session >= 5): PASSED** even in March.

### Fill Rate by Spread Bucket

| Bucket | Posts | Fills | Fill Rate | Fills/Day | Median TTF | P95 TTF |
|--------|-------|-------|-----------|-----------|------------|---------|
| 5-6 (marginal) | 5,070 | 1,496 | 29.5% | 74.8 | 0.9s | 14.7s |
| 7-10 | 2,904 | 1,248 | 43.0% | 62.4 | 3.0s | 56.7s |
| 11-20 | 4,274 | 1,598 | 37.4% | 79.9 | 16.9s | 95.3s |
| 20+ | 13,778 | 3,346 | 24.3% | 167.3 | 21.3s | 100.7s |

Key observations:
- Fill rate is highest in the 7-10 bucket (43%) and declines in the 20+ bucket (24.3%), suggesting very wide spreads are illiquid
- Time-to-fill increases dramatically with spread width: 0.9s median at 5-6 pts vs 21.3s at 20+
- P95 TTF reaches ~100s for wide spreads -- significant inventory exposure time

### Queue Position at Fill

| Bucket | P25 Queue | P50 Queue | P75 Queue |
|--------|-----------|-----------|-----------|
| 5-6 | 1 lot | 1 lot | 4 lots |
| 7-10 | 1 lot | 1 lot | 2 lots |
| 11-20 | 1 lot | 1 lot | 40 lots |
| 20+ | 1 lot | 26 lots | 40 lots |

Queue depth is bimodal: either very thin (1 lot ahead -- good position) or deep (26-40 lots -- likely won't fill before price moves).

### Cancel Reason Breakdown

| Reason | Count | % |
|--------|-------|---|
| Price moved up | 5,890 | 32.1% |
| Price moved down | 5,759 | 31.4% |
| Timeout (120s) | 5,420 | 29.6% |
| Spread narrowed | 1,245 | 6.8% |
| End of day | 24 | 0.1% |

~64% of cancels are due to price movement (roughly symmetric). Only 6.8% cancelled because spread narrowed below threshold. The 29.6% timeout rate suggests many wide-spread periods are genuinely illiquid -- orders sit for 2 minutes without filling.

---

## Winner's Curse Analysis

### Adverse Price Movement Post-Fill

| Bucket | % Adverse @1s | Mean @1s | % Adverse @5s | Mean @5s | % Adverse @30s | Mean @30s |
|--------|--------------|----------|---------------|----------|----------------|-----------|
| 5-6 | 33.0% | -0.10 pts | 45.6% | -0.46 pts | 49.2% | -0.02 pts |
| 7-10 | 29.2% | -0.27 pts | 42.2% | -0.71 pts | 50.2% | +0.02 pts |
| 11-20 | 14.3% | -0.35 pts | 25.3% | -0.79 pts | 46.4% | -1.71 pts |
| 20+ | 10.4% | -0.50 pts | 23.0% | -1.43 pts | 48.7% | -3.90 pts |

Key observations:
- **Adverse selection is surprisingly mild at short horizons** in tight spread buckets (5-6: 33% adverse at 1s)
- **Adverse selection increases with spread width** at all horizons. The 20+ bucket shows -3.90 pts adverse at 30s -- consistent with R16's "wide spread = informed flow" finding
- At 30s, all buckets converge to ~50% adverse -- consistent with random walk (signal dies)
- The 20+ bucket has the widest spreads but also the worst adverse selection, suggesting these are informationally loaded

### Net P&L Assessment (Spread Capture - Adverse Selection - RT Cost)

| Bucket | Avg Spread | Half-Spread | WC @30s | Net per Fill | Net per RT |
|--------|-----------|-------------|---------|-------------|-----------|
| 5-6 | 5.5 pts | 2.75 pts | -0.02 pts | +2.73 pts | **+1.5 pts** |
| 7-10 | 8.2 pts | 4.1 pts | +0.02 pts | +4.12 pts | **+4.2 pts** |
| 11-20 | 15.6 pts | 7.8 pts | -1.71 pts | +6.09 pts | **+9.9 pts** |
| 20+ | 48.5 pts | 24.25 pts | -3.90 pts | +20.35 pts | **+40.6 pts** |

*Net per RT = avg_spread - RT_cost(4) + WC@30s. Assumes instant completion of both legs.*

**Caveat**: The high net per RT in the 20+ bucket is deceptive -- these are overwhelmingly far-month observations. In March, there are almost no 20+ spread observations (0.2% of time).

---

## Fills Per Day (Regime Trend)

| Date | Fills | Notes |
|------|-------|-------|
| 2026-01-26 | 153 | Partial day |
| 2026-01-27 | 488 | |
| 2026-01-28 | 621 | |
| 2026-01-29 | 659 | |
| 2026-01-30 | 721 | |
| 2026-02-03 | 447 | |
| 2026-02-04 | 750 | |
| 2026-02-05 | 230 | |
| 2026-02-06 | 707 | |
| 2026-02-10 | 260 | |
| 2026-02-11 | 41 | |
| 2026-02-23 | 921 | |
| 2026-02-24 | 674 | |
| 2026-02-25 | 494 | |
| **2026-03-19** | **39** | Front month begins |
| **2026-03-20** | **84** | |
| **2026-03-23** | **172** | |
| **2026-03-24** | **142** | |
| **2026-03-25** | **64** | |
| **2026-03-26** | **21** | |

**Jan-Feb average: 512 fills/day. March average: 87 fills/day.** A 6x decline.

---

## Kill Gate Assessment

### Kill Gate 1: Fills per session >= 5
- **All days: PASSED** (384.4 avg, 21 minimum)
- **March only: PASSED** (87.0 avg, 21 minimum)

### Kill Gate 2: Winner's curse > spread capture for majority of fills
- **PASSED for 5-10 pt spreads** (adverse selection < spread capture)
- **MARGINAL for 20+ pt spreads** (high adverse selection but even wider spreads compensate)
- Note: 20+ bucket is almost entirely far-month data. Front-month assessment requires March-only rerun.

### NEW KILL GATE (from this analysis): Spread regime viability
- **CRITICAL WARNING**: The strategy's economic basis (45.5% spread >= 5) only holds for far-month contracts
- Front-month TMFD6 in March: only 6.15% of time has spread >= 5
- At 87 fills/day with 6.15% eligible time, the strategy operates in marginal conditions
- **This does not technically trigger the existing kill gates but fundamentally undermines the R18 premise**

---

## Conclusions and Recommendations

### What the data shows:

1. **Fill mechanics work**: 29.5% fill rate, sufficient fills/day, manageable queue positions
2. **Adverse selection is mild at tight spreads (5-10 pts)**: Only 33-42% adverse at 5s, consistent with uninformed flow
3. **Adverse selection increases with spread width**: 20+ pt spreads show 48.7% adverse at 30s with -3.90 pts mean move -- partially confirming R16's finding
4. **Net P&L per fill is positive across all buckets**: Even the marginal 5-6 bucket shows +1.5 pts net RT

### What the data invalidates:

1. **The 45.5% profitable-spread premise is an artifact of far-month contract mixing**. Front-month TMFD6 (March) has only 6.15% spread >= 5.
2. **The average profitable spread of 19.7 pts** drops to 7.7 pts in March. Net RT shrinks from ~16 pts to ~3.7 pts.
3. **The high fills/day count (384)** drops to 87 in March, and is trending downward (21 on March 26).

### Recommended action:

**The R18 team must decide: is this a far-month strategy or a front-month strategy?**

- **If far-month**: The economics are excellent (wide spreads, mild adverse selection, hundreds of fills/day). But far-month contracts have lower liquidity by definition, and the strategy is only viable ~2 months per quarterly cycle.
- **If front-month**: The economics collapse to TXFD6-like conditions (6% eligible time, tight spreads). This is exactly the regime R16 already rejected.
- **If always-front-month on TMF series**: Need to verify TMFC6 (March contract in Feb) and TMFB6 (February contract in Jan) show similar wide spreads when they were the far month. If yes, the strategy trades the *far* month and rolls quarterly.

This is a structural question that must be resolved before any strategy prototype work.

# Round 17 — OIDS Stage 2: TXO Data Diagnostic

**Date**: 2026-03-26
**Verdict**: **KILL — insufficient TXO tick data for OVI computation**

---

## Step 0: TXO Data Diagnostic Results

### 0.1 Row Count
- Total TXO rows in `hft.market_data`: **33,787,626** (33.8M)
- Passes the 1M threshold

### 0.2 Date Range
- First TXO record: **2026-01-21**
- Last TXO record: **2026-03-26**
- Span: ~65 days

### 0.3 Type Distribution
- **BidAsk events: ~33.67M** (~99.7% of all TXO data)
- **Tick events: ~115,643** (~0.3% of all TXO data)

**Critical finding**: The TXO data is overwhelmingly BidAsk quotes, NOT trade ticks. OVI requires TRADE volume (Tick events), not quote updates.

### 0.4 Non-Zero Volume Ticks
- Total TXO Tick events with volume > 0: **~115,643**
- This is **115x below the 1M threshold** from the kill gate

### 0.5 Symbol Encoding
TAIFEX standard encoding: `TXO<strike><month_code><year>`

| Suffix | Type | Expiry Month | Date Range in Data |
|---|---|---|---|
| B6 | Call | Feb 2026 | Jan 26 - Feb 23 |
| N6 | Put | Feb 2026 | Jan 22 - Feb 23 |
| C6 | Call | Mar 2026 | Feb 25 - Mar 18 |
| O6 | Put | Mar 2026 | Feb 25 - Mar 18 |
| D6 | Call | Apr 2026 | Mar 20 - Mar 26 |
| P6 | Put | Apr 2026 | Mar 20 - Mar 26 |

- **160 unique TXO symbols** across 3 expiry months
- Strikes range from ~22400 to ~34000 (put and call)
- Good strike coverage: 48 strikes per month for C6/O6 contracts

### 0.6 Buy/Sell Classification

**Schema check**: The `hft.market_data` table has NO explicit aggressor/buy/sell field. Fields available: `symbol, exchange, type, exch_ts, ingest_ts, price_scaled, volume, bids_price[], bids_vol[], asks_price[], asks_vol[], seq_no`.

**Lee-Ready feasibility**: YES. BidAsk events share identical timestamps with Tick events. We can match each Tick to the contemporaneous bid/ask quote and classify:
- Trade price > midpoint → buyer-initiated (buy)
- Trade price < midpoint → seller-initiated (sell)
- Trade price = midpoint → use tick rule (uptick = buy, downtick = sell)

Verified on sample: TXO32000O6 tick at price=389M with bid=389M, ask=450M → mid=419.5M → price < mid → SELL.

**However, this is moot because we don't have enough ticks.**

---

## Critical Overlap Analysis: TXO Ticks vs TMFD6 Ticks

### The Fatal Gap

| Date | TXO C6/O6 Ticks | TXO D6/P6 Ticks | TMFD6 Ticks | Overlap? |
|---|---|---|---|---|
| Feb 23 | 0 | - | 14,023 | NO |
| Feb 24 | 0 | - | 15,529 | NO |
| Feb 25 | 118 | - | 7,760 | MARGINAL |
| Mar 3-18 | 2,216-35,860 | - | **0** | **NO TMFD6** |
| Mar 19 | 0 | 0 | 195,244 | NO |
| Mar 20 | - | 216 | 305,565 | MARGINAL |
| Mar 23 | - | 1,257 | 402,306 | MARGINAL |
| Mar 24 | - | 1,671 | 256,913 | MARGINAL |
| Mar 25 | - | 1,951 | 166,700 | MARGINAL |
| Mar 26 | - | 452 | 45,754 | MARGINAL |

**When TXO has meaningful tick volume (Mar 3-18, C6/O6 contracts), TMFD6 has ZERO ticks.**
**When TMFD6 has meaningful tick volume (Mar 19+), TXO Apr contracts have only 216-1,951 ticks/day.**

### Root Cause
The TXO and TMFD6 data collection periods are misaligned. TMFD6 subscription was likely activated at different times or the data pipeline handled them separately. The March options (C6/O6) have their highest volume right before expiry (Mar 13: 22,739 ticks, Mar 16: 35,860 ticks) but TMFD6 has zero data during this period.

---

## Kill Gate Assessment

| Kill Gate | Threshold | Actual | Result |
|---|---|---|---|
| TXO non-zero-volume rows | >= 1M | 115,643 | **KILL** |
| Trading days overlap with TMFD6 | >= 20 | ~5 (marginal) | **KILL** |
| Buy/sell direction inferrable | Yes/No | Yes (Lee-Ready) | PASS |
| IC on overnight returns | >= 0.05 | Cannot compute | N/A |

**Two kill gates triggered:**

1. **TXO tick count: 115K << 1M required.** The 33.8M TXO rows are 99.7% BidAsk quotes, not trades. We have only ~115K trade ticks across all TXO options over 65 days.

2. **Overlap days: ~5 marginal days.** The days where both TXO ticks AND TMFD6 ticks exist have only 216-1,951 TXO ticks per day — far too few to compute a meaningful daily OVI across multiple strikes.

---

## Why 33M Rows Was Misleading

The Round 16 notes stated "33M rows untapped." This is technically true but the 33M rows are **BidAsk quote updates**, not trade ticks. BidAsk events capture the evolving order book but contain no information about trade direction or volume executed. OVI requires knowing:
1. A trade happened (Tick event)
2. The volume of the trade
3. Whether it was buyer- or seller-initiated

Only Tick events provide #1 and #2. Lee-Ready provides #3. But with only 115K Tick events total (and most of them during periods when TMFD6 has no data), OVI computation is not viable.

---

## Could BidAsk Data Be Used Instead?

One might consider using **BidAsk quote changes** as a proxy for trade direction (e.g., bid volume decrease = sell, ask volume decrease = buy). This is related to the OFI (Order Flow Imbalance) concept we already explored in R16.

However:
1. This is NOT OVI — it's OFI, which we already know doesn't work at 4pt cost (R16 dead zone)
2. Quote changes don't distinguish between cancellations and fills
3. Without trade volume, we can't weight by contract size or price

---

## Step 1: OVI IC Test (Attempted Despite Kill Gates)

Despite failing both kill gates, the IC test was attempted with the available 14 overlap observations.

### Symbol Encoding Verified
- TAIFEX standard: Call months = A-L (A=Jan...L=Dec), Put months = M-X (M=Jan...X=Dec)
- B6 = Feb Call, N6 = Feb Put, C6 = Mar Call, O6 = Mar Put, D6 = Apr Call, P6 = Apr Put
- Confirmed via price levels: TXO31500C6 avg ~139 pts (ITM call), TXO31500O6 avg ~17 pts (OTM put)

### Massive Put/Call Tick Asymmetry
| Suffix | Type | Ticks | Volume |
|---|---|---|---|
| B6 | Feb Call | 0 | 0 |
| N6 | Feb Put | 1,267 | 7,053 |
| C6 | Mar Call | 3,240 | 5,129 |
| O6 | Mar Put | 105,590 | 210,240 |
| D6 | Apr Call | 3,005 | 4,857 |
| P6 | Apr Put | 2,543 | 4,077 |

Put ticks outnumber call ticks ~20:1 (O6 dominates). Call tick data is severely sparse.

### Lee-Ready Classification
Applied successfully. Trade price vs BidAsk midpoint at same timestamp:
- price > mid → buyer-initiated (buy)
- price < mid → seller-initiated (sell)
- price = mid → excluded (tick rule fallback)

### OVI Results by Day

**Period 1: N6 only (Feb put, no calls available)**

| Date | OVI | Volume | Trades | TMFD6 Overnight Return |
|---|---|---|---|---|
| Jan 27 | -0.314 | 1,032 | 190 | +0.9 bps |
| Jan 28 | +0.731 | 4,270 | 454 | +0.3 bps |
| Jan 29 | +0.921 | 608 | 246 | -5.3 bps |
| Jan 30 | +0.750 | 96 | 92 | -2.2 bps |
| Jan 31 | +1.000 | 20 | 2 | +178.9 bps |
| Feb 3 | +0.000 | 20 | 16 | +0.0 bps |
| Feb 4 | +1.000 | 172 | 42 | -0.9 bps |
| Feb 5 | +0.891 | 258 | 70 | -125.7 bps |
| Feb 6 | +0.157 | 441 | 61 | +278.6 bps |
| Feb 10 | +0.356 | 59 | 59 | +108.3 bps |

Note: N6-period OVI is put-only (no call ticks), so interpretation is different from proper OVI. Very low volume days (20-96 contracts) produce extreme OVI values (+1.0, -0.31).

**Period 2: D6+P6 (Apr call+put, proper OVI)**

| Date | OVI | Bull Vol | Bear Vol | Trades | TMFD6 Overnight Return |
|---|---|---|---|---|---|
| Mar 20 | -0.177 | 114 | 163 | 212 | -3.7 bps |
| Mar 23 | +0.023 | 884 | 844 | 1,200 | -47.3 bps |
| Mar 24 | +0.097 | 1,684 | 1,385 | 1,585 | +46.9 bps |
| Mar 25 | +0.056 | 1,340 | 1,199 | 1,766 | -13.3 bps |

OVI in the D6/P6 period is very close to zero (range -0.18 to +0.10), indicating roughly balanced flow.

### IC Results

| Metric | Value |
|---|---|
| N observations | 14 |
| Rank IC (Spearman) | **+0.042** (p=0.887) |
| Pearson IC | **-0.030** (p=0.920) |
| OVI range | [-0.31, +1.00] |
| Return range | [-126, +279] bps |

**Both ICs are statistically insignificant (p >> 0.05) and below the 0.05 kill gate threshold.**

### Why the IC Test Is Meaningless
1. **N=14** is far below the 20-observation minimum. With 14 points, even a true IC of 0.50 would only be detected 40% of the time.
2. **N6-period OVI is put-only** — not a proper OVI (missing call component). These 10 observations are a different signal entirely.
3. **Extreme return outliers** (+278 bps, -126 bps, +179 bps) dominate the correlation — these are gap events unrelated to options flow.
4. **D6/P6-period OVI** (the only 4 days with proper call+put) is too close to zero to have any discriminative power at 4 observations.

---

## Recommendation

**OIDS candidate is KILLED.** Three kill gates triggered:

1. TXO tick count: 115K << 1M
2. Overlap days: 14 < 20 (and only 4 with proper call+put OVI)
3. IC = 0.042 < 0.05

**To revive OIDS in the future, we would need:**
1. Verify our Shioaji subscription includes TXO trade ticks (not just quotes)
2. Run for >= 30 trading days with TMFD6 simultaneously active
3. Accumulate >= 50K TXO ticks/day across all strikes (currently getting ~2K-36K only during near-expiry surge)

**Immediate pivot recommendation:**
- CSLL (Cross-Contract Lead-Lag) becomes the new Priority 1 candidate
- Requires subscribing to TXFD6 data, which is a simpler infrastructure change
- The lead-lag mechanism between TXFD6 and TMFD6 can be tested as soon as we start collecting TXFD6 data

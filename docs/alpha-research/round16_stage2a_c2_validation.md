# Round 16 Stage 2A: C2 Fill-Probability-Aware Contrarian Maker — Empirical Validation

**Date**: 2026-03-26
**Candidate**: C2 (Fill-Probability-Aware Contrarian Maker)
**Data**: TXFD6 L1, 13 trading days (2026-01-26 to 2026-03-24), 6.3M rows (3.6M during active trading)
**Verdict**: **KILL C2**

---

## Summary

| Check | Description | Result | Verdict |
|-------|-------------|--------|---------|
| V1 | Reversal magnitude after extreme imbalance | -0.46 ticks @40 (WRONG DIRECTION) | **FAIL** |
| V2 | Wide-spread reversal rate | 45.1% (wide) / 55.6% (narrow) | PASS |
| V3 | Tick resolution (Albers feasibility) | Median 1 tick/100ms | **FAIL** |
| V4 | Fill rate at contrarian touch @10 ticks | 12.5% (strict) | **FAIL** |
| V5 | Queue position from Shioaji | Not available | **FAIL** |
| V6 | Adverse fill prediction AUC | 0.519 (imbalance only) | **FAIL** |
| V7 | Economic model | Positive only due to inflated spread assumption | **FAIL** (revised) |

**Overall: 1 PASS / 6 FAIL -> KILL C2**

---

## V1: Reversal Magnitude After Extreme Imbalance

**THE CRITICAL FINDING: Extreme L1 imbalance predicts CONTINUATION, not reversal on TXFD6.**

Setup:
- Extreme imbalance = top/bottom 10% of L1 imbalance `(bid_qty - ask_qty) / (bid_qty + ask_qty)`
- Thresholds: `|imbalance| >= 0.5`
- Measured during active trading (spread < 10 bps), 3.57M ticks
- 856K extreme events (442K bearish, 414K bullish)

Results (combined, reversal = price moves against imbalance direction):

| Window | Mean Reversal | Median | % Correct Direction |
|--------|--------------|--------|---------------------|
| +10 ticks | **-0.434** | 0.0 | 37.8% |
| +20 ticks | **-0.448** | -0.5 | 42.1% |
| +40 ticks | **-0.459** | -0.5 | 44.9% |
| +80 ticks | **-0.385** | -0.5 | 46.7% |

**Interpretation**: The negative mean reversal means price moves IN THE SAME DIRECTION as the imbalance (continuation/momentum), not against it. The contrarian bet is wrong 62% of the time at the 10-tick horizon.

**PASS threshold was >= 2.0 ticks. Actual: -0.46 ticks. Not close -- FUNDAMENTAL FLAW.**

This contradicts the Round 15 depth_imbalance IC=-0.025 finding, which likely operates on a different timescale or aggregation. At tick-level granularity with L1 quantities, imbalance is a weak momentum signal, not a reversal signal.

---

## V2: Wide-Spread vs Narrow-Spread Reversal Rate

After a 1-tick price drop, what fraction reverse within 10 ticks?

| Condition | Reversal Rate | n |
|-----------|--------------|---|
| Wide spread (>2.5 bps) | 45.1% | 14,478 |
| Narrow spread (<=2.5 bps) | 55.6% | 741,520 |
| All | 55.4% | 755,998 |

**PASS** -- Wide-spread reversal rate 45% is above the 30% threshold and close to Albers' BTC benchmark (~45-50%). However, wide-spread events are only 1.9% of all price drops, so this is a very rare condition.

---

## V3: Tick Resolution Analysis

Median ticks per time window (active trading, per-day average across 4 recent days):

| Window | Mean | Median | P10 | P90 |
|--------|------|--------|-----|-----|
| 100ms | 1.5 | 1 | 1 | 2 |
| 5s | 45 | 41 | 27 | 65 |
| 30s | 265 | 245 | 167 | 365 |

**FAIL** -- Median 1 tick per 100ms means Albers' features (designed for high-frequency crypto with many ticks per 100ms) are not directly transferable. TXFD6 ticks arrive every ~125ms on average; there is insufficient intra-update microstructure for Albers-style feature extraction.

---

## V4: Fill Rate at Contrarian Touch

During extreme imbalance, if we place a limit order on the contrarian side:

### Strict fill (spread crossed -- ask drops to our bid or vice versa):

| Window | Buy Fill Rate | Sell Fill Rate |
|--------|--------------|----------------|
| 10 ticks | 12.5% | 12.6% |
| 20 ticks | 21.2% | 21.1% |
| 40 ticks | 30.5% | 30.5% |

### Soft fill (mid price touches our level):

| Window | Buy Fill Rate | Sell Fill Rate |
|--------|--------------|----------------|
| 10 ticks | 26.2% | 26.0% |
| 20 ticks | 34.8% | 34.7% |
| 40 ticks | 42.6% | 42.4% |

**FAIL** -- Strict fill at 10 ticks = 12.5% (threshold was 20%). Even the soft proxy is only 26%. At 40 ticks (5 seconds), strict reaches 30% but by then the signal has decayed.

**Critical implication**: Combined with V1 (reversal is negative), even the 12.5% that DO fill are filling into continuation, not reversal. The fills are adverse.

---

## V5: Queue Position Investigation

Searched `feed_adapter/shioaji/` for: `queue_pos`, `position_in`, `rank`, `priority`, `ahead`, `seq_no`

**Result**: No queue position data available from Shioaji SDK. Order acknowledgments contain order ID and status but no queue rank or position-ahead information.

**Impact**: Cannot estimate true fill probability or adverse selection from queue position. Must rely on visible depth as proxy, which understates queue disadvantage for retail (joins at back).

---

## V6: Ex-ante Adverse Fill Prediction

Logistic regression (imbalance-only, sklearn unavailable) to predict worst-20% post-fill returns:

- **AUC = 0.519** (on 500K samples)
- Threshold: 0.55
- **FAIL** -- L1 features have essentially no predictive power for adverse fills

This means we cannot selectively avoid toxic fills using observable LOB state at submission time.

---

## V7: Economic Model (Revised)

### With tight-spread filter (active trading):

| Parameter | Value |
|-----------|-------|
| Average mid price | 33,143 |
| Average spread (active) | 4.5 points |
| RT cost | 7.2 points (72 NTD) |
| Spread capture (half) | 2.25 points |
| Mean reversal @40 ticks | **-0.46 points** (NEGATIVE) |
| Exit slippage | 1.0 points |
| Queue adverse | 0.5 points |
| **Gross per fill** | **0.29 points** |
| **Net per fill** | **-6.9 points (-69 NTD)** |

**FAIL** -- With realistic active-trading spreads (4.5 points median) and negative reversal, the strategy loses ~69 NTD per fill. The initial V7 script output showed PASS due to including pre-market wide spreads (average spread 73 points), which inflated spread capture 16x.

Even without RT costs, the spread capture (2.25 pts) barely covers exit slippage (1.0) + queue adverse (0.5), leaving only 0.75 pts/trade before the **negative** reversal erases the remainder.

---

## Kill Reasons

1. **V1 (Fatal)**: Extreme L1 imbalance predicts CONTINUATION, not reversal. Mean reversal = -0.46 ticks (required >= 2.0). The entire C2 thesis is empirically invalidated.

2. **V4**: Fill rate at 10 ticks = 12.5% (required >= 20%). Even if reversal existed, fills come too slowly.

3. **V6**: Cannot predict adverse fills ex-ante (AUC = 0.52 ~ random). No way to avoid toxic fills.

4. **V7 (revised)**: Net loss of 69 NTD per filled trade during active trading hours.

5. **V3**: TXFD6 tick density too low for Albers-style microstructure features.

6. **V5**: No queue position data from broker, making fill probability estimation unreliable.

---

## Structural Insights for Future Research

1. **L1 imbalance is a weak momentum signal on TXFD6, not a reversal signal.** This is consistent with informed-trader-dominated microstructure: when bid queue is larger, informed buyers are accumulating; price follows.

2. **The Round 15 IC=-0.025 (reversal) for depth_imbalance likely operates at a different aggregation** -- possibly multi-level (L1-L5) or over longer horizons. Single-level L1 imbalance at tick frequency shows continuation.

3. **Contrarian market-making on TXFD6 is structurally challenged** because:
   - Tick density is too low for rapid signal update
   - No queue position transparency
   - 36ms broker RTT means we always join queue late
   - Spread is narrow during active hours (4.5 pts = 1.4 bps), leaving minimal capture room

4. **Wide-spread reversal rate (45%) is promising but too rare** (1.9% of events) to build a strategy around.

---

## Recommendation

**KILL C2. Do not proceed to prototype.**

The contrarian-maker thesis is empirically invalidated on TXFD6. L1 imbalance predicts continuation (momentum), not reversal, making the core signal directionally wrong. Combined with low fill rates, no queue position data, and negative economics, there is no viable path to profitability for this approach.

**Surviving candidates from Round 16 Stage 1 should focus on non-contrarian approaches** (e.g., C3 Adverse Selection Filter, which aims to AVOID adverse fills rather than trade against them).

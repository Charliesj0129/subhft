# R18 Stage 2b: SG-LP Backtest — Challenger Review

**Date**: 2026-03-26
**Reviewer**: Claude (Challenger Agent)
**Artifact reviewed**: `docs/alpha-research/round18_stage2b_sglp_backtest.md`
**Code reviewed**: `research/alphas/spread_gated_lp/{impl.py, backtest.py}`

---

## VERDICT: CONDITIONAL APPROVE

The strategy concept is sound and the per-fill economics are real, but three specific issues inflate the headline numbers. After adjustment, the strategy is still viable but at roughly half the reported P&L. Conditions for full approval are listed below.

---

## Challenge 1: Queue Depletion != Trade — Fill Count Overstated by 1.3-1.75x

**Claim challenged**: The backtest reports 872 fills/session (OOS) and treats every L1 queue depletion at our price level as a fill.

**Objection**: The dataset has `volume=0` for all rows (quote-only data, no trade markers). The backtest detects fills by observing `bid_qty` or `ask_qty` decrease at the same price level (`impl.py:196-199`). However, queue quantity can decrease for three reasons:

1. **Real trade** (counterparty hit/lifted our level) -- genuine fill
2. **Cancellation** by another participant -- NOT a fill
3. **Display/hidden order artifact** -- NOT a fill

I measured the classification across all March data:

| Depletion Type | Bid Side | Ask Side |
|----------------|----------|----------|
| With mid-price change (strong trade signal) | 35.7% | 35.4% |
| Price level gone next tick (probable trade-through) | 21.2% | 21.5% |
| **Ambiguous (no mid change, price persists)** | **43.1%** | **43.1%** |

**43% of all queue depletions are ambiguous** -- they could be cancellations by other participants, not real trades. Since the strategy's fill detection triggers on any depletion that advances our queue position to zero, a significant fraction of reported "fills" may be phantom.

**Impact estimate**: If 30-43% of fills are phantom, the overcounting factor is 1.3x to 1.75x:

| Scenario | OOS Fills/Day | OOS Daily NTD |
|----------|--------------|--------------|
| As reported | 872 | +38,470 |
| Conservative (1.3x) | 671 | +29,564 |
| Aggressive (1.75x) | 498 | +21,962 |

Even the aggressive adjustment leaves the strategy profitable at ~22K NTD/day, which still passes kill gates. But the headline +38K number is unreliable.

**Resolution required**: Either (a) obtain trade-tick data to distinguish real fills from cancellations, or (b) apply a 1.3x haircut to all fill counts and P&L as a conservative assumption.

**Severity**: HIGH. Does not kill the strategy but inflates reported performance by ~30-75%.

---

## Challenge 2: IS P&L is 84% Concentrated in One Anomalous Day (Mar 23)

**Claim challenged**: The backtest reports consistent IS/OOS performance with 25% IS/OOS gap for SG=5, OBI=0.

**Objection**: The per-day breakdown reveals extreme concentration:

| Day | Status | Fills | Eligible Time | Daily P&L (NTD) | % of Period Total |
|-----|--------|-------|--------------|-----------------|-------------------|
| Mar 19 | IS | 95 | 4.6 min (0.6%) | +4,355 | 1.2% |
| Mar 20 | IS | 891 | 93.1 min (1.7%) | +52,870 | 14.7% |
| **Mar 23** | **IS** | **5,156** | **42.0 min (12.3%)** | **+302,370** | **84.1%** |
| Mar 24 | OOS | 1,501 | 104.7 min (4.2%) | +61,920 | 53.7% |
| Mar 25 | OOS | 823 | 20.7 min (3.1%) | +40,050 | 34.7% |
| Mar 26 | OOS | 293 | 10.9 min (6.0%) | +13,330 | 11.6% |

**Mar 23 is a massive outlier**: 5,156 fills, 302K NTD, 84.1% of all IS P&L. It had 12.3% eligible time -- 2-20x more than any other day. This is likely an anomalous spread regime day (perhaps a volatility event that kept spreads wide for 42 minutes).

The IS average P&L per fill (+5.86 pts) is calibrated almost entirely on this one day. Without Mar 23, the IS average drops dramatically, and the IS/OOS "25% gap" becomes meaningless.

**OOS is also skewed** though less severely: Mar 24 contributes 53.7% of OOS P&L. The OOS CoV is 0.52 -- high for a 3-day sample.

With 3 IS + 3 OOS days and this variance, no statistical conclusion is possible:
- t-test for OOS mean > 0: t = 3.35, p = 0.039 (barely significant, 2 degrees of freedom)
- Mar 23 is a single-point leverage outlier that dominates IS calibration
- The declining trend (OOS: 61K -> 40K -> 13K) suggests either spread conditions are deteriorating or the strategy has a survivorship bias

**Resolution required**: (a) Report the per-day breakdown in the main results table. (b) Report IS statistics with and without Mar 23. (c) Collect at least 10 more trading days to assess stability.

**Severity**: HIGH. 3+3 days with this variance is insufficient for any promotion decision.

---

## Challenge 3: Eligible Time is Vanishing — Strategy Viability Window is Narrow

**Claim challenged**: The strategy is presented as viable for front-month TMFD6 trading.

**Objection**: The eligible time (spread >= 5) is small and declining across the sample:

| Day | Eligible Time |
|-----|--------------|
| Mar 19 | 0.6% (4.6 min) |
| Mar 20 | 1.7% (93 min -- includes overnight?) |
| Mar 23 | 12.3% (42 min -- anomalous) |
| Mar 24 | 4.2% (105 min -- includes overnight?) |
| Mar 25 | 3.1% (21 min) |
| Mar 26 | 6.0% (11 min -- partial day) |

Note: session durations of 765-1230 minutes suggest the data includes overnight periods or pre/post market. The actual regular session (08:45-13:45) is only 300 minutes. If eligible time is calculated on a 300-min session, the percentages roughly double -- but the absolute minutes remain small.

The fills-per-eligible-minute metric varies wildly: 9.6 to 122.8. This 13x variance suggests the strategy's throughput depends on a specific market microstructure condition (sustained wide spread with active trading) that occurs unpredictably.

**Resolution required**: Filter data to regular trading hours only (08:45-13:45 TWSE session). Report eligible time and fills within that window.

**Severity**: MEDIUM.

---

## Positive Findings (Not Challenged)

Several aspects of the backtest check out well:

1. **Latency impact is manageable**: Only 7.5-12.6% of fills occur within 65ms of posting. 87-93% of fills survive a 65ms latency penalty. Median time-in-queue is 149-181ms, well above the 65ms RTT.

2. **P&L per fill is consistent across spread buckets**: +2.59 pts (5-6 bucket) to +22.61 pts (20+ bucket), all positive. This is genuine spread capture, not noise.

3. **OBI adds no value**: Confirmed. The OBI filter reduces volume without improving per-fill P&L. This simplifies the strategy (no signal needed, pure spread gate).

4. **Win rates are stable**: 62-81% across spread buckets, consistent with spread capture mechanics (you profit whenever the spread is wide enough and adverse selection doesn't exceed the half-spread).

5. **Post-fill drift is near-zero**: Confirms this is spread capture, not a directional bet. The strategy's edge is structural (spread > cost), not predictive.

---

## Conditions for Approval

### Mandatory

1. **Report per-day P&L breakdown** in the main results table. The current format (aggregate IS/OOS) hides the extreme day-level variance. Include fills, eligible time, and daily NTD for each day.

2. **Apply 1.3x fill-count haircut** as a conservative adjustment for the queue-depletion ambiguity, until trade-tick data is available for validation. Report both raw and adjusted figures.

3. **Report IS statistics excluding Mar 23**. If the strategy is not profitable on Mar 19 + Mar 20 alone (which have only 0.6% and 1.7% eligible time), the entire IS calibration is based on one anomalous day.

### Recommended

4. **Collect 10+ additional trading days** before any promotion decision. The current 6-day sample is statistically meaningless for Sharpe estimation.

5. **Filter to regular trading hours** (08:45-13:45). The 1230-minute session durations suggest inclusion of overnight or pre-market data that may have different microstructure.

6. **Classify Mar 23**: What happened on this day? Was there a market event (earnings, macro) that caused sustained wide spreads? If so, the strategy may only be profitable during event-driven volatility -- not a steady-state edge.

---

## Summary Table

| # | Challenge | Severity | Key Finding |
|---|-----------|----------|------------|
| 1 | Queue depletion != trade; fills overstated 1.3-1.75x | HIGH | 43% of depletions are ambiguous (no mid change) |
| 2 | IS P&L 84% concentrated in Mar 23 anomaly; 3+3 days insufficient | HIGH | CoV=1.09 IS, 0.52 OOS; declining trend in OOS |
| 3 | Eligible time vanishing (0.6-12.3%); fills/min varies 13x | MEDIUM | Strategy viability depends on unpredictable spread events |

**Net assessment**: After a 1.3x haircut and excluding the Mar 23 anomaly, the strategy still appears marginally profitable (~20-30K NTD/day on good days, <5K on quiet days). The concept is validated -- spread-gated passive making on TMFD6 has positive expectation. But the headline +38K/day number is optimistic by roughly 2x, and the sample size is far too small for promotion.

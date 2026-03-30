# R18 Stage 2a: TMFD6 Reversal Frequency — BLOCKER-E4

**Date**: 2026-03-26
**Analyst**: Execution Agent
**Data**: TMFD6 BidAsk L1 from ClickHouse, 22 trading days (2026-01-27 to 2026-03-26), 7.85M events
**Script**: `research/experiments/validations/tmfd6_reversal/reversal_analysis_v2.py`

---

## BLOCKER-E4 Verdict: PASS — Direction A is NOT killed

**Reversal rate = 42-48%** across all tested conditions, far above the 10% kill threshold.

However, this result fundamentally changes the Direction A (RCM) thesis. Albers et al. report ~15% reversal rate on BTC perpetual. TMFD6 shows 42-48% — meaning OBI is a near-coin-flip predictor on TMFD6. This is not "occasional reversals to exploit" but rather "OBI barely works at all." The RCM strategy as described (filter for reversals) makes no sense when reversals are ~half of all events.

**Implication for Direction A**: The Albers reversal-conditional framework assumes a LOW base reversal rate (~15%) where a classifier can identify high-probability reversals. On TMFD6 with ~45% base rate, the problem is inverted: the strategy should filter for the ~55% of cases where OBI IS correct (momentum continuation), not the reversals. Direction A needs a fundamental redesign or should be deprioritized.

---

## 1. Spread Distribution

| Metric | Value |
|--------|-------|
| Total BidAsk events | 7,849,138 |
| Median spread | 7.0 pts |
| Mean spread | 20.9 pts |
| P25 / P75 | 3.0 / 33.0 pts |
| Spread >= 5 pts | **57.0%** |

| Spread bucket | % of time |
|--------------|-----------|
| 0-3 pts | 30.0% |
| 4 pts (breakeven) | 13.0% |
| 5-9 pts | 12.1% |
| 10-19 pts | 8.7% |
| 20-99 pts | 34.8% |
| 100+ pts | 1.3% |

**Note**: The survey cited 45.5% wide-spread time. Our measurement shows **57.0%** — the difference is likely due to different time filtering (the survey may have excluded pre/post-market or used a different spread threshold). The wider eligible window strengthens the SG-LP (Direction B) premise.

---

## 2. OBI Prediction Accuracy by Threshold and Horizon

Sampled every 20th BidAsk event across all spreads. "Predictions" = events where |OBI| > threshold.

| OBI threshold | Horizon | Predictions | Accuracy | Reversal rate |
|--------------|---------|-------------|----------|---------------|
| 0.0 | 1s | 247,551 | 54.9% | **45.1%** |
| 0.0 | 5s | 247,524 | 53.6% | **46.4%** |
| 0.0 | 30s | 247,402 | 54.6% | **45.4%** |
| 0.1 | 5s | 233,646 | 53.6% | 46.4% |
| 0.2 | 1s | 202,967 | 55.9% | 44.1% |
| 0.2 | 5s | 202,942 | 54.2% | 45.8% |
| 0.3 | 5s | 192,239 | 54.4% | 45.6% |
| **0.5** | **1s** | 108,249 | **57.8%** | **42.2%** |
| **0.5** | **5s** | 108,235 | **56.8%** | **43.2%** |
| **0.5** | **30s** | 108,171 | **59.7%** | **40.3%** |

### Key observations

1. **OBI is barely predictive on TMFD6**: Even at OBI > 0.5, accuracy is only 57.8% at 1s — compared to Gould & Bonart's ~60-65% on NASDAQ.
2. **Reversal rate is 42-48%**: 3x higher than Albers' 15% on BTC perpetual. The "reversal" concept does not transfer.
3. **Higher thresholds help but not enough**: OBI > 0.5 improves accuracy to ~58%, but loses 56% of predictions.
4. **No clear horizon sweet spot**: Accuracy roughly flat across 1s/5s/30s. Best: 59.7% at 30s with OBI > 0.5.

---

## 3. Reversal Rate by Spread Bucket

At OBI > 0.0, horizon = 5s:

| Spread (pts) | Sampled | Predictions | Accuracy | Reversal rate |
|-------------|---------|-------------|----------|---------------|
| 0-3 | 117,901 | 96,588 | 52.2% | **47.8%** |
| 4 (breakeven) | 50,876 | 39,612 | 51.3% | **48.7%** |
| 5-9 | 47,484 | 26,489 | 51.8% | **48.2%** |
| 10-19 | 34,227 | 24,107 | **59.5%** | 40.5% |
| 20-99 | 136,726 | 57,600 | **63.9%** | **36.1%** |
| 100+ | 5,252 | 3,128 | **66.2%** | **33.8%** |

### Key finding: OBI accuracy improves dramatically at wide spreads

- **Tight spreads (0-4 pts)**: OBI is near-random (51-52%). Reversal rate ~48%.
- **Wide spreads (20-99 pts)**: OBI accuracy 63.9%. Reversal rate 36.1%.
- **Very wide (100+ pts)**: OBI most accurate (66.2%) but only 5,252 events.

**Implication for SG-LP**: Consider raising spread gate from >= 5 to >= 10 pts. At 5-9 pts, OBI accuracy is only 51.8%. At 10-19 pts it jumps to 59.5%.

---

## 4. Reversal Rate by Time of Day

At OBI > 0.0, horizon = 5s:

| Period | Sampled | Predictions | Accuracy | Reversal rate |
|--------|---------|-------------|----------|---------------|
| 08:45-09:15 | 23,242 | 14,761 | 52.7% | 47.3% |
| 09:15-10:00 | 25,770 | 16,717 | 52.4% | 47.6% |
| 10:00-11:00 | 23,012 | 14,709 | 52.9% | 47.1% |
| 11:00-12:00 | 19,171 | 10,716 | 53.9% | 46.1% |
| 12:00-13:00 | 14,208 | 8,274 | 52.2% | 47.8% |
| 13:00-13:45 | 8,626 | 5,976 | 51.8% | 48.2% |

**No strong time-of-day effect.** OBI accuracy stable at 52-54% across the day.

---

## 5. Spread >= 5 Only: Reversal by Horizon

| Horizon | Predictions | Accuracy | Reversal rate |
|---------|-------------|----------|---------------|
| 1s | 111,337 | 57.5% | 42.5% |
| 5s | 111,324 | 57.8% | 42.2% |
| 30s | 111,274 | **60.8%** | **39.2%** |

OBI accuracy at wide spreads: ~58% (vs ~54% unconditional). Supports using OBI as directional skew at wide spreads.

---

## 6. BLOCKER-E5: Tick Rule Feasibility

| Metric | Value |
|--------|-------|
| Ticks sampled (5 days) | 13,084 |
| Classified (uptick/downtick) | 5,143 (**39.3%**) |
| Zero-tick (unclassifiable) | 7,941 (**60.7%**) |
| Avg ticks/day | 2,617 |

### Verdict: TICK RULE NOT FEASIBLE on TMFD6

60.7% of consecutive trades occur at the same price. Albers' "recent trades" feature group cannot be replicated. Only LOB-state features are available for any reversal/momentum classifier on TMFD6.

---

## 7. Summary and Recommendations

### Kill gate results

| Gate | Threshold | Result | Verdict |
|------|-----------|--------|---------|
| BLOCKER-E4 (reversal rate) | < 10% = kill A | 42-48% | **PASS** (not killed, but thesis inverted) |
| BLOCKER-E5 (tick rule) | feasible? | 39.3% classifiable | **FAIL** — full reversal classifier not buildable |

### Strategic implications

1. **Direction A (RCM) needs fundamental reframing**: OBI is near-random on TMFD6 (~54% accuracy). The "detect rare reversals" approach from Albers does not apply. Reframe as: use OBI as momentum-confirmation signal at wide spreads (64% accuracy at 20-99 pts spread).

2. **Direction B (SG-LP) is strengthened**: 57% wide-spread time. Recommended: raise spread gate to >= 10 pts where OBI accuracy jumps to 59.5%. Time in spread >= 10: ~44.8%.

3. **New insight: spread regime is THE key variable**, not OBI threshold or time-of-day. OBI accuracy ranges from 51% (tight spread) to 66% (very wide spread) — a 15 percentage point swing driven entirely by spread regime.

### Key numbers for Stage 2 parameterization

- OBI accuracy at spread >= 10: ~60%
- OBI accuracy at spread >= 20: ~64%
- Reversal rate at spread >= 10: ~37%
- Time in spread >= 10: ~44.8%
- Time in spread >= 5: 57.0%
- Avg ticks/day: 2,617
- Tick rule classification rate: 39.3% (not viable)

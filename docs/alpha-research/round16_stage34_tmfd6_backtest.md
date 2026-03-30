# Round 16 Stage 3-4: TMFD6 OpportunisticMM Data Analysis + Backtest

**Date**: 2026-03-26
**Symbol**: TMFD6 (Micro-TAIEX Futures, 微台指期)
**Data**: 7.74M BidAsk events, 21 trading days (2026-01-26 to 2026-03-26)

## Executive Summary

**VERDICT: TMFD6 OpportunisticMM is NOT viable. ALL configurations produce deeply negative PnL.**

The core finding: despite TMFD6 having much wider spreads than TXFD6 (median 3-43 pts vs 1 pt), the one-sided adverse selection rate is 93-100%. When our posted bid gets hit (bid level consumed by aggressive seller), the price almost always continues in the same direction. We get filled exactly when we don't want to be filled. This is the textbook adverse selection problem that defeats naive market-making.

## 1. TMFD6 Data Summary

### Daily Statistics

| Day | Regime | Ticks | Med Spread | >=5 pts | >=10 pts | Med Interval | Bid Vol | Ask Vol |
|-----|--------|------:|----------:|---------:|---------:|------------:|--------:|--------:|
| 2026-01-26 | WIDE | 31,213 | 30 | 99.1% | 96.9% | 250 ms | 18.8 | 13.5 |
| 2026-01-27 | WIDE | 221,172 | 28 | 99.4% | 96.6% | 250 ms | 16.6 | 16.1 |
| 2026-01-28 | WIDE | 282,763 | 26 | 99.4% | 94.9% | 250 ms | 22.0 | 24.1 |
| 2026-01-29 | WIDE | 431,030 | 38 | 99.5% | 97.4% | 125 ms | 23.8 | 26.8 |
| 2026-01-30 | WIDE | 504,658 | 43 | 99.7% | 98.1% | 125 ms | 27.7 | 28.1 |
| 2026-01-31 | WIDE | 184,268 | 42 | 99.3% | 97.4% | 125 ms | 8.0 | 8.2 |
| 2026-02-03 | WIDE | 185,647 | 31 | 99.7% | 96.3% | 125 ms | 27.1 | 30.0 |
| 2026-02-04 | WIDE | 562,520 | 38 | 99.5% | 97.2% | 125 ms | 21.2 | 21.1 |
| 2026-02-05 | WIDE | 423,676 | 52 | 99.9% | 98.9% | 125 ms | 21.1 | 23.3 |
| 2026-02-06 | WIDE | 481,528 | 36 | 98.8% | 95.8% | 125 ms | 26.7 | 23.5 |
| 2026-02-10 | WIDE | 69,311 | 31 | 98.8% | 96.3% | 375 ms | 13.9 | 13.7 |
| 2026-02-11 | WIDE | 9,576 | 21 | 98.6% | 86.0% | 749 ms | 13.1 | 23.0 |
| 2026-02-23 | MED | 475,814 | 7 | 91.3% | 30.2% | 125 ms | 2.0 | 2.0 |
| 2026-02-24 | MED | 430,956 | 6 | 80.4% | 12.9% | 125 ms | 2.0 | 2.1 |
| 2026-02-25 | MED | 200,250 | 6 | 81.9% | 7.0% | 125 ms | 2.0 | 2.2 |
| 2026-03-19 | TIGHT | 516,982 | 3 | 0.6% | 0.1% | 104 ms | 4.6 | 5.1 |
| 2026-03-20 | TIGHT | 783,520 | 3 | 1.7% | 0.3% | 100 ms | 5.7 | 5.2 |
| 2026-03-23 | TIGHT | 824,541 | 3 | 12.3% | 2.1% | 80 ms | 3.3 | 3.4 |
| 2026-03-24 | TIGHT | 610,036 | 3 | 4.2% | 0.4% | 88 ms | 4.1 | 4.1 |
| 2026-03-25 | TIGHT | 444,684 | 3 | 3.1% | 0.8% | 108 ms | 4.5 | 4.5 |
| 2026-03-26 | TIGHT | 73,669 | 3 | 6.0% | 0.3% | 79 ms | 4.4 | 4.0 |

**Critical observation**: There is a dramatic regime change between Jan-Feb (WIDE, median 21-52 pts) and March (TIGHT, median 3 pts). This likely reflects a market maker entering TMFD6 in late February, compressing spreads from 20-50 pts down to 3 pts. The original "45.5% of time spread >= 5 pts" statistic was dominated by the early WIDE regime.

In the current March regime, spread >= 5 pts only 1-12% of the time.

### Regime Timeline

```
Jan-Feb (IS):  WIDE regime — median spread 21-52 pts, tick interval 125-750ms
               L1 queue depth 8-28 contracts, very thin
               spread >= 5pts: 98-99% of time

Late Feb:      MED regime — median spread 6-7 pts, possible MM entry
               L1 queue depth 2 contracts (!)
               spread >= 5pts: 80-91%

March (OOS):   TIGHT regime — median spread 3 pts, active MM present
               L1 queue depth 3-6 contracts
               spread >= 5pts: 1-12%
```

## 2. Spread Distribution

### Jan 30 (WIDE regime)
```
Flat distribution from 5-50+ pts. No dominant level.
  5 pts:  0.4%
 10 pts:  0.7%
 20 pts:  1.6%
 30 pts:  1.7%
 40 pts:  2.1%
 50 pts:  1.5%
>50 pts: 35.9%
```

### Feb 24 (MED regime, transition)
```
Peak at 5-7 pts. Tight but still workable.
  3 pts:  6.4%
  4 pts: 10.2%
  5 pts: 17.3%  <-- peak
  6 pts: 21.4%  <-- peak
  7 pts: 13.7%
  8 pts:  8.4%
```

### Mar 23 (TIGHT regime, current)
```
Dominated by 3-4 pts. RT cost = 4 pts. Structurally unprofitable.
  1 pts:  4.8%
  2 pts: 14.6%
  3 pts: 33.3%  <-- mode
  4 pts: 35.1%  <-- mode
  5 pts:  5.3%
  6 pts:  2.2%
```

## 3. Backtest Results

### Parameters
- **RT cost**: 4 points (40 NTD = tax 7 + comm 13 per side)
- **Point value**: 10 NTD/point
- **Fill model L1**: Bid filled when bid level drops (optimistic)
- **Fill model L2**: Bid filled when price drops through level (conservative)
- **Close model**: Immediate market close on next tick after one-sided fill
- **IS period**: Jan 26 - Feb 11 (12 days, WIDE regime)
- **OOS period**: Feb 23 - Mar 26 (9 days, MED+TIGHT regime)

### Summary Table (L1 fill model)

| Threshold | Period | Days | Trades | Both-Fill | 1-Side | Net PnL (pts) | NTD/day | Sharpe | 1s Adv% | Gross/RT |
|-----------|--------|-----:|-------:|----------:|-------:|---------------:|--------:|-------:|--------:|---------:|
| 5 | IS | 12 | 12,985 | 100 | 12,885 | -157,475 | -131,229 | -23.2 | 100% | -8.1 |
| 5 | OOS | 9 | 57,010 | 333 | 56,677 | -346,022 | -384,469 | -17.0 | 96% | -2.1 |
| 10 | IS | 12 | 11,934 | 89 | 11,845 | -138,344 | -115,287 | -22.9 | 100% | -7.6 |
| 10 | OOS | 9 | 7,169 | 66 | 7,103 | -51,265 | -56,961 | -15.0 | 95% | -3.2 |
| 20 | IS | 12 | 8,448 | 75 | 8,373 | -92,403 | -77,002 | -21.4 | 100% | -6.9 |
| 20 | OOS | 9 | 791 | 16 | 775 | -8,380 | -9,311 | -12.9 | 96% | -6.6 |
| 30 | IS | 12 | 5,658 | 66 | 5,592 | -60,403 | -50,336 | -20.1 | 100% | -6.7 |
| 30 | OOS | 9 | 381 | 8 | 373 | -3,652 | -4,058 | -11.7 | 94% | -5.6 |
| 40 | IS | 12 | 3,713 | 50 | 3,663 | -38,730 | -32,275 | -18.3 | 100% | -6.4 |
| 40 | OOS | 9 | 191 | 2 | 189 | -2,008 | -2,231 | -12.2 | 98% | -6.5 |

### Summary Table (L2 fill model)

| Threshold | Period | Days | Trades | Both-Fill | 1-Side | Net PnL (pts) | NTD/day | Sharpe | 1s Adv% | Gross/RT |
|-----------|--------|-----:|-------:|----------:|-------:|---------------:|--------:|-------:|--------:|---------:|
| 5 | IS | 12 | 11,848 | 91 | 11,757 | -152,882 | -127,402 | -23.1 | 100% | -8.9 |
| 5 | OOS | 9 | 30,778 | 73 | 30,705 | -227,235 | -252,483 | -17.1 | 98% | -3.4 |
| 10 | IS | 12 | 10,870 | 82 | 10,788 | -134,053 | -111,711 | -22.8 | 100% | -8.3 |
| 10 | OOS | 9 | 4,152 | 25 | 4,127 | -37,521 | -41,690 | -15.3 | 96% | -5.0 |
| 20 | IS | 12 | 7,721 | 71 | 7,650 | -89,568 | -74,640 | -21.3 | 100% | -7.6 |
| 20 | OOS | 9 | 487 | 9 | 478 | -6,672 | -7,413 | -13.8 | 96% | -9.7 |

**L2 is slightly worse** than L1 because the additional fill strictness reduces volume without improving adverse selection.

## 4. Adverse Selection Analysis

### Markout Analysis (spread transition from <5 to >=5)

When the spread widens from <5 to >=5 points, what happens to mid-price?

| Horizon | Count | Mean Move (pts) | Std (pts) | t-stat | Bid Adverse | Ask Adverse |
|---------|------:|----------------:|-----------:|-------:|------------:|------------:|
| 0.1s | 23,214 | +0.007 | 3.46 | 0.32 | 24.6% | 24.0% |
| 0.5s | 23,214 | +0.015 | 4.79 | 0.47 | 38.2% | 37.7% |
| 1.0s | 23,214 | +0.013 | 6.11 | 0.33 | 41.7% | 41.0% |
| 2.0s | 23,213 | +0.001 | 8.11 | 0.02 | 44.7% | 43.1% |
| 5.0s | 23,204 | +0.139 | 13.0 | 1.62 | 46.6% | 46.2% |
| 10.0s | 23,194 | +0.264 | 19.0 | 2.12 | 48.1% | 47.6% |

**Interpretation**: The markout at spread-widening transitions is essentially zero (mean ~0 pts, t-stat <2 at short horizons). This means spread widening is NOT informationally neutral -- it's random noise. However, the key problem is not at the transition, it's at the fill.

### Why One-Sided Fills Are Catastrophic

The 93-100% adverse selection rate on one-sided fills is the core problem:

1. We post at best bid. The bid level gets consumed (someone sells aggressively).
2. This aggressive sell is almost always the START of a price move, not a random blip.
3. When we close on the next tick, the bid has already moved 5-10+ pts against us.
4. Our average gross PnL per RT is **-6 to -8 pts** (negative!) before costs.

The "both-fill" rate is tiny: <1% of trades are perfect round trips where both legs fill simultaneously. The spread is wide enough that it rarely gets crossed simultaneously from both sides in one tick.

### Comparison: One-Sided Fill Economics

```
Average gross PnL per one-sided RT: -6 to -8 pts (WIDE regime), -2 to -3 pts (TIGHT regime)
RT cost: 4 pts
Average net PnL per RT: -10 to -12 pts (WIDE), -6 to -7 pts (TIGHT)

At 10 NTD/pt: -100 to -120 NTD per trade (WIDE), -60 to -70 NTD per trade (TIGHT)
```

Even ignoring costs entirely, the gross PnL is deeply negative. **The strategy has negative alpha before costs.**

## 5. IS/OOS Comparison

| Configuration | IS Net/day (pts) | OOS Net/day (pts) | IS Sharpe | OOS Sharpe |
|---------------|------------------:|-------------------:|----------:|-----------:|
| L1, thr=5 | -13,123 | -38,447 | -23.2 | -17.0 |
| L1, thr=10 | -11,529 | -5,696 | -22.9 | -15.0 |
| L1, thr=20 | -7,700 | -931 | -21.4 | -12.9 |
| L1, thr=30 | -5,034 | -406 | -20.1 | -11.7 |
| L1, thr=40 | -3,228 | -223 | -18.3 | -12.2 |

Higher thresholds lose less per day in OOS, but only because there are far fewer trades in the TIGHT regime. The per-trade economics remain deeply negative everywhere.

## 6. Structural Analysis: Why TMFD6 OpMM Fails

### The Illusion of Wide Spreads

The original thesis was: "TMFD6 has 45.5% of time with spread >= 5 pts, which covers the 4-pt RT cost."
This is fatally flawed because:

1. **Regime instability**: The wide-spread regime (Jan-Feb) collapsed to tight spreads (Mar) as a market maker entered. The 45.5% figure is a period average, not a stable property.

2. **Adverse selection dominates**: Wide spreads exist precisely BECAUSE the instrument is illiquid. Illiquid instruments have severe adverse selection -- the only people willing to cross wide spreads are informed traders with directional conviction.

3. **Queue depth is 2-5 contracts**: At L1 depth of 2-5, ANY single aggressive order consumes the entire level. There is no "queue priority" because there is no queue. Every fill is an aggressive sweep.

4. **BidAsk data shows price impact, not fills**: A bid dropping below our level means the entire bid level was consumed. This is not a random fill -- it's a directional move.

### The Market Maker Paradox

In the current TIGHT regime (Mar), a professional market maker has already entered TMFD6:
- Spread compressed from 20-50 pts to 3 pts
- L1 depth increased from 2 to 4-6 contracts
- Tick rate increased from 1.8/s to 8-12/s

This MM likely has:
- Colocation or very low latency (sub-millisecond)
- Inventory management across TXFD6/TMFD6/options
- Maker rebates or fee structure advantages we don't have
- Hedging capability against the parent TXFD6 contract

We cannot compete with this MM. Our 36ms RTT means we're always stale by 3-4 BidAsk updates.

### Comparison with TXFD6

| Metric | TXFD6 | TMFD6 (Current) | TMFD6 (Jan-Feb) |
|--------|------:|----------------:|-----------------:|
| Median spread | 1 pt | 3 pts | 26-52 pts |
| RT cost (pts) | 0.7 | 4 | 4 |
| Spread/cost ratio | 1.4x | 0.75x | 6-13x |
| Spread >= profitable | 2.1% | 1-12% | 86-99% |
| L1 depth | 50-100 | 3-6 | 8-28 |
| Tick rate | 8/s | 8-12/s | 1-8/s |
| **OpMM viable?** | **NO** | **NO** | **NO** |

Despite TMFD6 having a much better spread/cost ratio on paper, the adverse selection is catastrophically worse. TXFD6's deep order book (50-100 contracts at L1) provides some queue insulation. TMFD6's thin book (2-5 contracts) provides none.

## 7. Conclusion

### TMFD6 OpportunisticMM: DEAD

- **Sharpe**: -11.7 to -23.2 across all configurations (profoundly negative)
- **One-sided adverse selection**: 93-100% (fills are uniformly poisonous)
- **Both-fill rate**: <1% (almost never earn the spread)
- **Gross PnL**: Negative before costs (-2 to -8 pts per RT)
- **Regime stability**: WIDE regime collapsed to TIGHT within 1 month

### Why This Strategy Cannot Be Rescued

1. **No threshold saves it**: From 5 to 40 pts, all negative. Higher thresholds simply reduce volume while keeping per-trade losses.
2. **No fill model saves it**: L1 and L2 both produce 93-100% adverse selection.
3. **No passive close saves it**: Waiting for the other side to fill adds inventory risk without improving entry adverse selection.
4. **The problem is structural**: TMFD6 is too illiquid for retail MM. Every fill is adversely selected because there's no queue buffer.

### Surviving Options for TMFD6

1. **Directional signals only**: Use TMFD6 as a low-cost expression of a directional signal from TXFD6. Cost = 4 pts RT (40 NTD) vs 0.7 pts (35 NTD) on TXFD6, but TMFD6 has 1/5 the point value, so notional cost per NTD is similar.

2. **Cross-instrument arbitrage**: TMFD6 vs TXFD6 price divergence. Requires monitoring both instruments and fast execution. Basis risk is minimal (same underlying index).

3. **Drop TMFD6 entirely**: Concentrate on TXFD6 where liquidity provides better execution quality for signal-based strategies.

## Artifacts

- Data extraction: `research/experiments/validations/tmfd6_opmm/extract_data.py`
- Backtest script: `research/experiments/validations/tmfd6_opmm/backtest_numba.py`
- Raw results: `research/experiments/validations/tmfd6_opmm/results_final.json`
- Data files: `research/experiments/validations/tmfd6_opmm/data/tmfd6_*.npz` (21 days)

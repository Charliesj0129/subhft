# Round 16 MC-6: Execution Optimization via Reversal-Timed Entry

**Date**: 2026-03-26
**Data**: TXFD6 L1 tick data, 4 days (2026-03-19 to 2026-03-24), 1,779,257 ticks

---

## Concept

Reframe the reversal signal as an **execution optimization tool** rather than standalone alpha. If a trader has a planned buy/sell, can they save slippage by timing entry to reversal windows?

This does not need to overcome RT costs independently -- it just needs to improve execution quality of trades that would happen anyway.

---

## Part 1-2: Slippage Comparison (Random vs Reversal-Timed)

Simulated buying 1 contract by posting limit buy at bid, waiting up to 30s for fill, falling back to taker if unfilled.

| Strategy | Fill Rate | Blended Savings vs Taker |
|----------|-----------|--------------------------|
| A: Immediate taker at ask | 100% | 0 pts (baseline) |
| B: Passive at bid (any time) | 72.1% | 1.44 pts/trade |
| C: Passive at bid (reversal-timed) | 73.5% | 1.47 pts/trade |
| D: Passive + reversal + safe OFI | 67.0% | 1.34 pts/trade |

**Key finding**: Passive vs taker saves ~1.4 pts/trade. But reversal timing adds only +0.03 pts/trade over unconditional passive. The toxic flow filter actually HURTS (reduces fill rate without improving quality).

---

## Post-Fill Markout Analysis (Adverse Selection Check)

For all passive fills, measured mid-price movement AFTER fill:

| Condition | 5s Markout | 10s Markout | 30s Markout | % Positive at 30s |
|-----------|-----------|-------------|-------------|-------------------|
| All fills | -2.5 pts | -2.5 pts | -2.5 pts | 41.3% |
| Buy-favorable imbalance | -2.5 pts | -2.5 pts | -3.0 pts | 41.0% |
| Buy-unfavorable imbalance | -2.5 pts | -2.5 pts | -2.5 pts | 41.5% |
| Neutral imbalance | -2.5 pts | -2.5 pts | -2.5 pts | 41.4% |

**Adverse selection is uniform**: Regardless of imbalance condition, passive fills show ~2.5 pts adverse movement. Only 41% of fills show positive markout at 30s. This confirms the Albers et al. finding: **maker fills are adversely selected by construction** (you get filled because someone sold into you, which means selling pressure exists).

Notably, imbalance conditioning does NOT reduce adverse selection. "Buy-favorable" fills (ask-heavy imbalance) show the same -2.5 to -3.0 pts markout as unfavorable fills.

---

## Part 3: Annual Slippage Savings Model

### TXFD6 (Full Contract, 200 TWD/pt)

| Trades/Day | Passive vs Taker Savings | Reversal-Timed Savings | Marginal Benefit of Timing |
|-----------|--------------------------|------------------------|---------------------------|
| 5 | 353,290 TWD/yr | 360,150 TWD/yr | 6,860 TWD/yr |
| 10 | 706,580 TWD/yr | 720,300 TWD/yr | 13,720 TWD/yr |
| 50 | 3,532,900 TWD/yr | 3,601,500 TWD/yr | 68,600 TWD/yr |
| 100 | 7,065,800 TWD/yr | 7,203,000 TWD/yr | 137,200 TWD/yr |

### XMT (Mini-TAIEX, 50 TWD/pt)

| Trades/Day | Passive Savings | Timed Savings | Marginal |
|-----------|----------------|---------------|----------|
| 10 | 176,645 TWD/yr | 180,075 TWD/yr | 3,430 TWD/yr |
| 50 | 883,225 TWD/yr | 900,375 TWD/yr | 17,150 TWD/yr |
| 100 | 1,766,450 TWD/yr | 1,800,750 TWD/yr | 34,300 TWD/yr |

---

## Part 4-5: Combined Assessment

The toxic flow filter (avoiding high-|OFI| periods) **hurts execution** by reducing fill rate from 73.5% to 67.0% without improving post-fill markout. During "toxic" periods, there is more trading activity, which paradoxically means MORE fills and SIMILAR markout quality.

---

## MC-6 Verdict

### What Works
- **Passive > Taker**: Savings of 1.4 pts/trade (288 TWD on TXFD6, 72 TWD on XMT) are significant and real. At 50 trades/day on TXFD6, this is 3.5M TWD/year.

### What Does Not Work
- **Reversal timing**: Marginal benefit of +0.03 pts/trade (5.6 TWD on TXFD6) is negligible. Not worth the implementation complexity.
- **Toxic flow avoidance**: Reduces fill rate without improving quality. Net negative.
- **Imbalance conditioning**: Zero impact on post-fill adverse selection. All fills show -2.5 pts median markout regardless of imbalance state.

### Root Cause
The "reversal signal" from L1 imbalance has only 52.4% accuracy at 5s and decays to 50.5% at longer horizons. This is insufficient to produce meaningful execution timing alpha. The main driver of execution quality is simply **patience** (waiting for passive fill vs paying the spread), not **timing** (choosing when to be patient).

### Recommendation
1. **Implement passive execution as standard practice** -- this alone saves 1.4 pts/trade
2. **Do not invest in reversal-timing infrastructure** -- marginal benefit is negligible
3. **The reversal signal from raw L1 imbalance is too weak for any use case** -- standalone alpha, execution timing, or filtering. The direction requires either richer data (trade-by-trade) or fundamentally different features.

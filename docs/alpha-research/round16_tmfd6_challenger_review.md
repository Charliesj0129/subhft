# Round 16 TMFD6 Feasibility -- Challenger Review

**Date**: 2026-03-26
**Reviewer**: Challenger agent
**Artifact reviewed**: `outputs/team_artifacts/alpha-research/round16_tmfd6_feasibility.md`
**Script reviewed**: `research/scripts/round16_tmfd6_feasibility.py`

---

## Per-Candidate Verdicts

| Candidate | Researcher Verdict | Challenger Verdict | Notes |
|:----------|:-------------------|:-------------------|:------|
| C: Fill Probability Filter | REJECTED | **APPROVE (rejection)** | OOS AUC=0.523 is indeed random. Rejection is correct. |
| A: Inventory Skew Optimization | REJECTED | **CONDITIONAL** | R2=0.077 is damning, but the step-function claim needs more scrutiny. See Challenge #1. |
| B: Toxic Flow Detection | REJECTED | **APPROVE (rejection)** | 60% toxic rate at 2-pt threshold is clearly below TMFD6 normal vol. Rejection is correct. |
| Simple OpMM recommendation | P0 priority | **CONDITIONAL** | The "3 pt gross edge" and "1,500 RTs/day" claims are both overstated. See Challenges #2, #3, #4. |

---

## Challenge #1: Step-Function Fill Rate -- Data Artifact or Structural?

### The claim

The Researcher reports that fill rate vs. spread follows a two-regime step function (3.5/s at tight spreads, ~0.15/s flat at 5+ pts) rather than exponential decay, with R2=0.077 for the exponential fit. This is presented as a "structural finding" about TMFD6 microstructure.

### The problem

Looking at the fill rate table:

| Spread (pts) | Fill rate (/s) |
|:------------|---------------:|
| 1-3 | 3.498 |
| 3-5 | 1.059 |
| 5-7 | 0.169 |
| 7-10 | 0.122 |
| 10-15 | 0.151 |
| 15-20 | 0.135 |
| 20-30 | 0.139 |
| 30-50 | 0.188 |
| 50-100 | 0.255 |

The "flat" regime at 5+ pts actually shows a U-shaped pattern: rates decline from 0.169 to 0.122, then rise back to 0.255 at the widest spreads. The rising tail (30-50: 0.188, 50-100: 0.255) is suspicious. At 50-100 pt spreads, fills should be extremely rare if the market is genuinely wide. Two possible explanations:

1. **Temporal confound**: Wide spreads predominantly occur during specific periods (market open/close, volatility events). The March 20 and 23 dates have sigma=6-7.5 pts/sqrt(s) -- roughly 3x the Feb dates. If wide-spread fills cluster on volatile days, the "fill rate at wide spreads" is measuring "fill rate during volatile sessions" rather than "fill rate as a function of distance from mid." The Researcher's methodology pools all dates when computing time-at-spread and fills-at-spread, which conflates temporal regimes with spread regimes.

2. **Fill definition issue**: A tick at price >= best_ask is counted as a "buy fill," but at 50-100 pt spreads, a tick hitting the ask likely represents an aggressive market order sweeping through thin liquidity -- not a passive maker being filled. The fill simulation does not distinguish between trades that would fill a resting limit order and trades that represent aggressive sweeps. At wide spreads, the "fills" may be dominated by sweeps where a passive maker would NOT have been at that price level.

### Required data to resolve

- **DATA REQUEST #1**: Break the fill rate table by date. If the wide-spread fill rates are driven entirely by March 20 and 23 (the high-vol days), the "step function" may be a regime-mixing artifact. Specifically, compute the fill rate table for Feb 23-26 only (the 4 "normal" days) and compare to March 20-23 (the 2 "volatile" days).

- **DATA REQUEST #2**: For fills at spread >= 20 pts, report the distribution of fill prices relative to mid-price. If fills at wide spreads cluster near mid (rather than near BBO), they are aggressive sweeps that would not fill a passive limit order resting at BBO.

### Impact on verdict

If the step function is a date-mixing artifact, the exponential model may actually hold within homogeneous liquidity regimes, which would partially rehabilitate Candidate A for the "normal" regime. This does not change the overall rejection (6 days is insufficient for any model calibration), but it changes the structural conclusion about TMFD6 microstructure.

---

## Challenge #2: The "3 pt Gross Edge" Claim is Overstated

### The claim

The report states: "Median spread of 7 pts vs RT cost of 4 pts leaves 3 pts gross edge (75% of cost) when quoting at BBO." This is the foundation of the Simple OpMM recommendation.

### The problems

**Problem A: Half-spread, not full-spread capture.** A maker quoting at BBO on one side captures half the spread per leg. A round-trip requires BOTH legs to fill. The gross capture per RT is therefore the full spread (7 pts) minus the RT cost (4 pts) = 3 pts, BUT only if both legs fill at the same spread level. In practice:

- The bid fill and ask fill occur at different times. The spread may have changed between fills.
- If spread narrows between legs, the second leg captures less. If it widens, you capture more, but you also waited longer (carrying inventory risk).
- The median spread is 7 pts, but the spread distribution is right-skewed (mean 7.7, P75=8). A maker enters at a moment of wide spread and may exit at a narrower spread.

**Problem B: Adverse selection eats into gross edge.** The Researcher's own data shows 23.5% adverse fill rate. The mean post-fill return is +0.432 bps, which at a mid-price of ~21,000 pts equals roughly +0.09 pts. So the average single fill is nearly breakeven, NOT capturing 3.5 pts (half-spread). The +1.48 pts reported mean return (Section 2 of the report) is the mid-price return, NOT the realized P&L per fill.

**Critical distinction**: The post-fill return measures mid-price movement after a fill, not the actual P&L of the trade. A maker who buys at bid and sees mid go up by 1.48 pts has NOT made 1.48 pts -- they still need to sell (the second leg), and the adverse-selection risk applies to that leg too.

### What the data actually shows

The correct way to estimate gross edge per RT is:

```
Gross edge = spread_at_entry - spread_consumed_by_adverse_selection - RT_cost
```

With 23.5% adverse rate and mean adverse return likely in the -3 to -5 bps range (the report does not break this out), the expected single-leg capture is significantly less than half-spread. The "3 pt gross edge" assumes zero adverse selection, which the Researcher's own Section 2 data contradicts.

**DATA REQUEST #3**: Report the mean post-fill return in POINTS (not bps) for adverse fills specifically (the 23.5%). This allows computing the expected value: `E[capture] = 0.765 * E[return | favorable] + 0.235 * E[return | adverse]`. If the adverse fills lose an average of 3+ pts, the net edge may be near zero.

---

## Challenge #3: "1,500 RTs/day" Estimate is Unrealistic

### The claim

"If 10% of ~15,000 daily fills result in completed round-trips: ~1,500 RTs/day"

### The problems

1. **The 15,000 "fills" are not all fillable by one maker.** With L1 depth of 1 contract per side, the queue has only 1 contract. If we place our order, we ARE the queue (or we are behind 1 contract). At most 50% of "fills at BBO" would fill our order if we are at queue front. More realistically, with other participants competing for the same 1-contract queue depth, our fill probability per BBO touch is much lower.

2. **The 10% RT completion rate is unjustified.** A round-trip requires both legs to fill before inventory risk materializes. With a thin book and fills at ~0.15/s (the 5+ pt spread regime), completing a 2-leg RT takes significant time. The Researcher provides no data on how long an average RT takes or what fraction of first-leg fills can be closed within a reasonable time window.

3. **Position limits.** TMFD6 is a mini contract. Retail traders may face position limits or margin constraints that prevent holding 1,500 RTs of inventory turnover per day. The report does not address this.

### Realistic estimate

With 1-contract queue depth and realistic queue priority assumptions:
- Daily fills achievable by one maker: ~1,000-3,000 (not 15,000)
- RT completion rate (both legs within 60s): probably 20-40% (not 10%)
- Realistic RTs: 200-1,200/day, with significant variance

This is not fatal to the opportunity, but the P&L projections (15,000 NTD/day) should be revised down by 2-5x.

---

## Challenge #4: IS/OOS Adverse Rate Shift Undermines All Results

### The claim

The Researcher notes the IS/OOS adverse rate shifts from 18.2% to 31.3% (a 72% relative increase) and correctly identifies this as "non-stationary." However, the analysis then proceeds to draw conclusions about TMFD6 microstructure as if the 6-day dataset is representative.

### The problem

A 13 percentage point shift in adverse fill rate across 6 days means:
- The Feb 23-26 period and March 20-23 period represent fundamentally different regimes.
- The Feb dates have sigma ~2.0-2.9 pts/sqrt(s); March dates have sigma ~6.1-7.6 pts/sqrt(s). That is a 2-3x volatility jump.
- All "average" statistics (23.5% overall adverse rate, 7 pt median spread, 0.15/s fill rate at wide spreads) are regime-weighted averages that may not represent ANY actual trading regime.

This is particularly concerning for the Simple OpMM recommendation. If the strategy is calibrated on the 6-day average, it will:
- Over-quote during the low-vol regime (Feb), where adverse rates are genuinely 18% and spreads are narrower
- Under-quote during the high-vol regime (March), where adverse rates are 31% but spreads are much wider

### Assessment

The 6-day dataset (4 days normal, 2 days volatile) is **insufficient for any production-quality conclusion**. The Researcher should have been more explicit that these results are preliminary and regime-dependent. The "3 pt gross edge" calculation, in particular, may hold only in the low-vol regime and collapse in the high-vol regime where adverse selection doubles.

---

## Challenge #5: Fill Simulation Double-Counting Risk

### Code-level finding

Looking at lines 193-198 of the script:

```python
at_bid = tick_price <= book_best_bid[book_idx]
at_ask = tick_price >= book_best_ask[book_idx]
is_fill = at_bid | at_ask
```

This counts ANY tick at or through the BBO as a "fill." Two issues:

1. **Through-the-book trades**: A tick at price < best_bid (below bid) gets counted as a fill. This could be a delayed tick from a previous book state, or a trade that swept multiple levels. These are aggressive take-liquidity events, not passive maker fills.

2. **Multiple ticks at same price**: If 10 contracts trade at the best_bid in rapid succession, all 10 are counted as fills. But a maker with 1 contract at BBO would only be filled once. The fill count is thus inflated by the volume at each price level.

The 101,488 fills across 6 days (16,900/day) probably overstates the realistic fill count for a 1-contract maker by 3-10x. This directly inflates the "1,500 RTs/day" estimate.

**DATA REQUEST #4**: Report the number of UNIQUE BBO-level transitions (i.e., the number of times price touches bid or ask with at least one intervening book update) rather than raw tick count. This gives a more realistic upper bound on single-contract fills.

---

## Assessment of the "Simple OpMM" Recommendation

### What the Researcher got right

1. TMFD6's wider spreads and lower RT cost create a structurally more favorable environment for MM than TXFD6. This is correct.
2. The comparison: TXFD6 median spread (4 pts) vs RT cost (7 pts) = negative edge; TMFD6 median spread (7 pts) vs RT cost (4 pts) = positive edge. This directional conclusion is sound.
3. Using TMFD6 as a proving ground before TXFD6 is sensible given the lower notional risk (10 NTD/pt vs 200 NTD/pt).

### What the Researcher got wrong or overstated

1. **The gross edge is not 3 pts.** After adverse selection, the expected net capture per RT is likely 0.5-1.5 pts, not 3 pts. Still positive, but the margin of safety is much smaller.
2. **1,500 RTs/day is unrealistic.** 200-600 RTs/day is more defensible for a single 1-contract maker.
3. **Revised daily P&L**: At 400 RTs/day * 1 pt net = 400 pts = 4,000 NTD/day. This is micro-scale but still worth testing.
4. **Queue priority is THE unknown.** With L1 depth of 1 contract, queue position is binary -- either we are first or we are not. The report does not address the competitive landscape (how many other MMs are active on TMFD6?).

### Verdict on OpMM recommendation: CONDITIONAL

The structural opportunity is plausible but the quantitative claims need 3 corrections before proceeding:
1. Compute realized spread capture (both legs), not just single-leg post-fill return
2. Estimate realistic fill rate for a single-contract maker (not total market fills)
3. Extend data to at least 20 trading days to separate regime effects

---

## Summary of Challenges and Data Requests

| # | Challenge | Data Request | Severity |
|:-:|:----------|:-------------|:---------|
| 1 | Step-function fill rate may be date-mixing artifact | Break fill rate table by date (normal vs volatile days) | MEDIUM |
| 2 | "3 pt gross edge" ignores adverse selection | Report mean return (pts) for adverse fills specifically | HIGH |
| 3 | "1,500 RTs/day" assumes all market fills are available | Report unique BBO-touch count, not raw tick count | HIGH |
| 4 | IS/OOS adverse shift (18.2% to 31.3%) undermines regime-averaged conclusions | Split all statistics by low-vol (Feb) vs high-vol (March) regime | HIGH |
| 5 | Fill simulation counts through-the-book and multi-tick fills | Filter to first tick at each BBO level per book update | MEDIUM |

## Overall Assessment

The 3 candidate rejections are **all correct** -- the Researcher did solid work showing these alpha overlays fail on TMFD6. No objection there.

The positive finding (TMFD6 as a structurally viable MM venue) is **directionally correct but quantitatively overstated**. The "3 pt gross edge at 1,500 RTs/day = 15,000 NTD/day" headline should be revised to approximately "0.5-1.5 pt net edge at 200-600 RTs/day = 1,000-5,000 NTD/day." This is still worth exploring but sets more realistic expectations.

**Recommendation**: Proceed with Simple OpMM prototype on TMFD6 ONLY after addressing Data Requests #2, #3, and #4. Do NOT use the current 6-day statistics for parameter calibration -- collect at least 20 days of data first.

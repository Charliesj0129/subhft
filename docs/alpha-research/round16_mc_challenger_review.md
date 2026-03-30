# Round 16 MC-1~4: Challenger Review of Validation Results

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Document Under Review**: `docs/alpha-research/round16_mc_validation_results.md`

---

## Verdict: REJECT (1 fatal error, 2 major concerns)

The validation contains a **fatal cost model error** in MC-1 that invalidates the core feasibility conclusion. MC-2 and MC-4 have statistical methodology issues that make their "STRONG PASS" verdicts unreliable. MC-3 passes but with an unexpected result that weakens the combined strategy thesis.

---

## MC-1: REJECT -- Fatal Cost Model Error

### The Error

The validation claims TXFD6 futures round-trip cost is ~0.49 bps:
> "TAIFEX futures transaction tax: ~0.004% per side (~0.8 bps RT)"
> "Commission (Shioaji discount): ~0.15 pts per side (~0.3 bps RT)"
> "Total TXFD6 round-trip: ~1.62 index points (~0.49 bps)"

**This is wrong.** The established and user-confirmed cost structure is:

| Component | Correct Value | Source |
|-----------|--------------|--------|
| Sell-side transaction tax | 2.0 bps (on sell side only) | `feedback_taifex_fee_structure.md`, user-confirmed |
| Commission | 60 NTD/contract RT (~0.3 bps) | Same source |
| **Total RT** | **2.3 bps** | Round 14, Round 15, both Stage 1 surveys |

The validation's "~0.004% per side" figure appears to confuse TAIFEX futures tax with some other instrument's tax rate. The TAIFEX futures transaction tax is 0.00002 (2 basis points) on the sell side, NOT 0.00004% per side. This has been confirmed across:

- `feedback_taifex_fee_structure.md`: "Sell-side tax: 2.0 bps"
- `docs/superpowers/plans/2026-03-25-p1-tca-pnl-attribution.md`: `tax_rate_bps: 2.0`, `tax_side: sell`
- The Researcher's own survey (`round16_stage1_survey.md`): "2.0 bps sell tax" (mentioned 4 times)
- The second survey (`round16_stage1_qfin_tr_survey.md`): "Round-trip cost: 2.3 bps (comm 0.3 + tax 2.0)"
- Round 14 report: "2.3 bps futures RT (comm 0.3 + tax 2.0)"

### Impact on MC-1 Verdict

With the correct 2.3 bps RT cost, the break-even accuracy table becomes:

| Horizon | Cost (pts) | Median Move | Min Accuracy |
|---------|-----------|-------------|-------------|
| 5s | ~7.6 pts | 4.0 pts | **147%** (IMPOSSIBLE) |
| 10s | ~7.6 pts | 5.5 pts | **119%** (IMPOSSIBLE) |
| 15s | ~7.6 pts | 6.5 pts | **108%** (IMPOSSIBLE) |
| 30s | ~7.6 pts | 9.5 pts | **90%** (near-impossible) |

Cost calculation: 2.3 bps * (index ~33,000) / 10000 * point_value_factor. At index ~20,000, 2.3 bps = ~4.6 pts. At index ~33,000, 2.3 bps = ~7.6 pts.

Even at the most generous horizon (30s), the required accuracy of ~90% is practically unreachable for a logistic regression model on noisy LOB features. The Albers paper achieved strong results only WITH maker rebates on Binance, where break-even accuracy was much lower.

**MC-1 corrected verdict: FAIL.** The strategy cannot overcome 2.3 bps RT cost at any reasonable horizon with achievable prediction accuracy. This is exactly the scenario my original challenge predicted ("If the required accuracy is >70%, this is likely infeasible").

### Note on Edge Cases

The ONLY scenario where MC-1 might survive: if the strategy captures the FULL spread (not just half) on wide-spread ticks. At spread > 2.5 bps, we already know OpMM earns +0.32 bps/RT (Round 13). The reversal detector would need to lift that from +0.32 to >+2.3 bps -- a 7x improvement. This is extremely unlikely from a binary reversal classifier alone.

---

## MC-2: CONCERNS -- Statistical Methodology Issues

### 2.1: Data Scope Too Narrow (4 days)

The analysis uses only 4 "clean" days (March 19-24), excluding Jan/Feb data as anomalous. This is concerning:

- 4 days is insufficient for queue depth statistics. TXFD6 queue behavior varies by session type (regular vs. rollover), day of week, and macro event proximity.
- The Jan/Feb "wide spread" anomaly should be investigated, not excluded. If those days represent a different market regime, our strategy must handle that regime too (or at least detect and avoid it).
- N=4 independent daily samples cannot establish distributional properties reliably.

### 2.2: "Thin Side Queue = 1 Contract" Claim Needs Scrutiny

The finding that 56% of reversals have thin-side queue depth = 1 contract is surprisingly convenient. This means our back-of-queue position is #2, and the reversal consuming the thin side would fill us.

But this requires careful verification:

- **Definition of "thin side"**: Is this the side with fewer contracts? If spread is 1 tick and the imbalance predicts UP, the "thin" ask (fewer contracts) would need to be consumed for a reversal (price going DOWN). But a reversal means the price moves AGAINST the imbalance prediction. If the thick bid (more contracts) is being consumed instead, our ask-side order would need to be on the thin side to benefit. The directionality needs explicit definition.

- **Queue snapshot timing**: At 125ms median tick interval and 36ms RTT, we see the queue state 36ms ago. If a reversal is already in progress, the thin-side queue depth we observe may be stale -- the queue may have already been partially consumed by the time our order arrives.

- **Survivorship in the 88.6% fill rate**: The 88.6% "fill rate" is computed as the fraction of inter-price-change intervals > 36ms. But this is NOT the same as fill probability. Having enough time to submit an order does not mean the order will be filled. The order must still be at or better than the prevailing price when the reversal trade arrives. If the price has already moved by the time our order reaches the exchange (36ms later), we may be placing an order at a now-stale price.

### 2.3: Reversal Definition Matters

The validation defines reversals as "imbalance wrong" (39.6% of ticks). But the reversal concept from Albers et al. is more specific: cases where the LOB imbalance falsely predicts the NEXT price change. The validation's 39.6% reversal rate is for the unconditional case. The reversal classifier should select a SUBSET of these where conditions are favorable. The base rate matters because it determines the prior probability for the classifier.

### MC-2 Revised Verdict: INCONCLUSIVE

The queue depth finding is plausible but needs:
1. Expand to more than 4 days
2. Clarify thin-side queue definition relative to reversal direction
3. Separate "time to submit order" from "order fills at submitted price"

---

## MC-3: PASS (with important twist)

### The Inverse OFI-Volatility Relationship

The finding that OFI IC is strongest in LOW volatility (Q1: IC=0.076) and weakest in HIGH volatility (Q5: IC=0.038) passes the >0.02 threshold but has a 2x ratio (0.076/0.038), just barely meeting my original 2x condition.

### Concern: Conflict with Combined Strategy

The inverse relationship creates a fundamental tension with Candidate #1:
- Reversal detection (#1) needs price movement to generate fills and PnL. Reversals are more frequent and larger during volatile periods.
- OFI regime filtering (#2) says to trade only in calm periods.
- These two signals point in opposite directions.

If we combine them, we either:
1. Trade in calm periods (OFI strong, but few reversals and small moves -- insufficient PnL per trade)
2. Trade in volatile periods (more reversals, but OFI weak -- poor signal quality)

This is not fatal for Candidate #2 standalone, but it weakens the "combine all three" thesis.

### MC-3 Verdict: PASS but weakens combined strategy

---

## MC-4: CONCERNS -- Overly Optimistic Funnel Model

### 4.1: 7,220 Fills/Day is Unrealistic

The funnel model arrives at 7,220 fills/day. This is orders of magnitude higher than what any selective maker strategy should expect. For comparison:
- The Albers paper (BTC perpetual, 24/7, massive liquidity, co-located) placed 232,897 orders over an unspecified but multi-week period.
- Our OpportunisticMM in sim mode generates far fewer fills per day.

The funnel model's errors:

1. **"75% safe periods"** -- This assumes toxic flow is only 25% of the time. No empirical basis is provided. Given that TXFD6 is a futures contract with institutional participants, informed flow could be 40-60% of volume.

2. **"20% high-confidence"** -- This is the selectivity filter, which should be much more aggressive. If we're targeting >90% accuracy (needed per corrected MC-1), the selectivity must be <5%, not 20%.

3. **"88.6% fill rate"** -- As discussed in MC-2, this conflates "time to submit" with "order fills." Actual fill rate for back-of-queue limit orders is likely 10-30% per signal, not 88.6%.

4. **Missing: position management time** -- After each fill, we hold inventory that must be unwound. The unwind trade also costs 2.3 bps. During the unwind period, we cannot take new entries.

### 4.2: Corrected Funnel (using MC-2 concerns + corrected MC-1)

| Stage | Filter | Remaining |
|-------|--------|-----------|
| Price changes/day | -- | 187,634 |
| Reversals (39.6%) | -- | 74,343 |
| Independent signal windows (15s avg) | non-overlapping | 1,200 |
| Toxic flow filter (50% safe) | -- | 600 |
| Classifier selectivity (5%, targeting >90% accuracy) | -- | 30 |
| Actual fill probability (25%) | -- | **~8 fills/day** |

At 8 fills/day, even with a generous +1.0 bps net edge per fill (unlikely after 2.3 bps cost), daily PnL = 8 * 1.0 bps * ~6.6 NTD/bps = ~53 NTD/day. This is negligible.

### MC-4 Revised Verdict: FAIL at corrected cost assumptions

---

## Summary

| Check | Validation Verdict | Challenger Verdict | Key Issue |
|-------|-------------------|-------------------|-----------|
| MC-1 | CONDITIONAL PASS | **FAIL** | Fatal cost model error: used 0.49 bps instead of 2.3 bps |
| MC-2 | STRONG PASS | **INCONCLUSIVE** | 4-day sample, fill rate != submission time, queue definition unclear |
| MC-3 | PASS | **PASS** (with twist) | OFI strongest in low vol -- conflicts with reversal strategy needs |
| MC-4 | STRONG PASS (7,220/day) | **FAIL** | Corrected funnel: ~8 fills/day at viable accuracy thresholds |

### Root Cause

The entire validation rests on the incorrect cost assumption of ~0.49 bps RT. At the correct 2.3 bps RT cost:

1. Break-even accuracy becomes 90-147% (impossible at short horizons)
2. The classifier must be hyper-selective to have any chance (5% not 20%)
3. Hyper-selectivity collapses trade frequency to single digits per day
4. Single-digit daily trades cannot generate meaningful PnL

### Recommendation

**The MC-1 cost error must be corrected and all downstream analyses re-run before Stage 2 can proceed.** If the corrected analysis confirms that break-even accuracy at 10-30s horizons exceeds 85%, the combined #1+#3 strategy should be KILLED and Round 16 should pivot to:

1. **Candidate C (Fill Probability Filter)** from the Execution Review -- approved as an OpMM incremental improvement, does not require directional prediction
2. **Candidate A (Latency-Aware Skew)** from the Execution Review -- also approved, composes with OpMM

Both of these are execution optimization candidates that improve the existing OpMM strategy without requiring the strategy to overcome 2.3 bps RT costs independently. They are incremental improvements to an already-marginal strategy, which is the realistic scope given our cost constraints.

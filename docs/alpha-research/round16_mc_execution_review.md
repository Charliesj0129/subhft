# Round 16 MC-1~4: Execution Review of Validation Results

**Reviewer**: Execution Agent
**Date**: 2026-03-26
**Document reviewed**: `docs/alpha-research/round16_mc_validation_results.md`

---

## MC-1: Minimum Reversal Prediction Accuracy

### Cost Model Correction: PARTIALLY VALID, NEEDS RECONCILIATION

MC-1 claims TXFD6 futures costs are ~0.49 bps RT (7x cheaper than stocks). I verified the actual platform config:

**Platform config** (`config/base/fees/futures.yaml`):
- TX product: `tax_rate_bps: 2.0`, `tax_side: sell`, `commission_per_contract: 60`, `point_value: 200`
- TXFD6 maps to TX via `symbol_map` (TXF -> TX)

**MC-1 claims**: TAIFEX futures tax ~0.004% per side (~0.8 bps RT), commission ~0.15 pts/side.

**Regulatory reality**: TAIFEX index futures transaction tax is 0.00002 per side (0.2 bps per side, 0.4 bps RT on sell side only). The MC-1 figure of ~0.8 bps RT appears to double-count (both sides taxed), but TAIFEX only taxes the sell side. The actual tax is ~0.4 bps per RT.

**Platform config discrepancy**: Our config says `tax_rate_bps: 2.0` for TX futures. This is **the TWSE stock sell tax rate, not the TAIFEX futures rate**. If the platform uses this config in live trading, it will over-estimate costs by ~5x. However, I also found that TXFD6 is NOT in the `symbol_map` -- only TXF/TXFL5/TXFR1 are mapped. The `_resolve_product()` method in `FeeCalculator` will return `None` for "TXFD6", causing it to return **ZERO fees**. This is a fee calculator bug.

**Corrected cost model**:

| Component | Per RT | Source |
|-----------|--------|--------|
| TAIFEX transaction tax (sell only) | ~0.4 bps | Regulatory rate 0.00002 |
| Commission (60 NTD/contract x2) | ~0.3 bps | `config/base/fees/futures.yaml` at ~20,000 pts notional |
| **Total** | **~0.7 bps** | |

MC-1's claim of ~0.49 bps is slightly optimistic (under-counts commission), but the order of magnitude is correct. The corrected figure is ~0.7 bps RT, still 5x cheaper than the 3.5 bps stock cost originally assumed.

**Break-even accuracy at corrected costs**:

| Horizon | Cost (pts) | Median Move | Min Accuracy |
|---------|-----------|-------------|--------------|
| 5s | ~2.3 pts | 4.0 pts | ~78.8% |
| 10s | ~2.3 pts | 5.5 pts | ~70.9% |
| 15s | ~2.3 pts | 6.5 pts | ~67.7% |
| 30s | ~2.3 pts | 9.5 pts | ~62.1% |

These are higher than MC-1's figures but still in an achievable range (60-79% depending on horizon).

### MC-1 Verdict: PASS with corrections

- The fundamental finding (futures costs << stock costs) is correct and changes the viability calculus
- The exact cost figure needs correction (~0.7 bps, not ~0.49 bps)
- Break-even accuracy is ~3-8% higher than MC-1 claimed but still achievable
- **ACTION REQUIRED**: Fix `config/base/fees/futures.yaml` tax_rate_bps for TX (should be ~0.2 bps, not 2.0) AND add TXFD6 to `symbol_map`

---

## MC-2: Queue Depth and Back-of-Queue Fill Rate

### Analysis: STRONG PASS (confirmed)

MC-2 findings are internally consistent and match platform constraints:

1. **88.6% of price changes have >36ms lead time**: This means we can post a limit order and have it arrive before the next price change 88.6% of the time. Verified against our P95 submit RTT of 36ms from `config/research/latency_profiles.yaml`.

2. **Median thin-side queue depth = 1 contract**: This is the critical enabler. If the queue is only 1 contract deep, then back-of-queue is position 2. During a reversal, the price moves THROUGH the thin side, consuming ALL queued contracts. Position 2 gets filled.

3. **56% of reversals have queue depth = 1**: This means our order is frequently the ONLY order at that level (queue position 1 if we arrive before anyone else, position 2 otherwise).

### Execution Concerns

- **P95 vs P50 latency**: MC-2 uses our P95 submit RTT (36ms) as the threshold. The actual median (P50) RTT is likely ~15-20ms, improving the fill window further. However, for risk management, P95 is the correct threshold.

- **Order cancellation**: If the reversal does NOT materialize (the 60.4% case where imbalance correctly predicts direction), we need to cancel. Cancel P95 is 47ms. With 130ms median inter-change time, we have ~83ms margin after recognizing the non-reversal. This is tight but workable.

- **Multiple participants**: MC-2 assumes we are the only back-of-queue joiner. If other participants also detect the reversal opportunity, queue depth grows and fill rate drops. This is an adversarial dynamic not captured in historical analysis.

### MC-2 Verdict: PASS

The thin queue structure of TXFD6 is a genuine structural advantage that makes back-of-queue viable. This was the critical unknown from my Stage 1 review, and the data strongly supports feasibility. The 88.6% fill rate far exceeds the 5% kill threshold.

---

## MC-3: OFI IC by Volatility Quintile

### Analysis: PASS (with important caveat)

1. **Max quintile IC = 0.076 (Q1, low vol)**: Exceeds the 0.02 kill threshold by 3.8x. Candidate #2 survives.

2. **Inverse relationship (IC decreases with volatility)**: This is a significant finding that contradicts the Hu & Zhang (2025) CSI 300 results. On TXFD6, OFI is most predictive during calm periods.

### Execution Implications

- **Regime-conditional OFI is INVERSELY useful for the reversal strategy**: The reversal strategy (Candidate #1) benefits from activity (more reversals occur during active periods). But OFI is strongest during calm periods. This means the two signals are somewhat anti-correlated in their optimal operating regimes.

- **IC = 0.076 is still weak**: Even the best quintile IC (0.076) translates to very small directional edge. At 0.7 bps RT cost, need to assess whether this IC can generate net positive returns after costs. IC of 0.076 on a 10s horizon with median 5.5 pts move gives expected edge of ~0.076 * 5.5 = ~0.42 pts per signal. At ~2.3 pts RT cost, this is still insufficient standalone.

- **Candidate #2 as standalone**: REJECT remains my recommendation. IC = 0.076 is above the kill threshold but below the profitability threshold. The kill threshold was intentionally conservative (0.02); surviving it does not mean profitability.

- **Candidate #2 as feature input to #1**: APPROVE. OFI regime can serve as one input to the reversal classifier, particularly for identifying calm periods where book signals are more reliable.

### MC-3 Verdict: PASS (kill gate only; not a profitability endorsement)

---

## MC-4: Trade Frequency Model

### Analysis: STRONG PASS (confirmed)

1. **7,220 fills/day at base assumptions**: 144x above the 50-fill threshold.

2. **Funnel model is reasonable**:
   - 54,330 reversals/day (29% of 187,634 price changes) -- consistent with MC-2's 39.6% reversal rate on the subset with imbalance data
   - 75% safe periods (toxic flow filter) -- conservative assumption
   - 20% selectivity (high-confidence reversals) -- this is the key tuning parameter
   - 88.6% fill rate -- from MC-2

3. **Sensitivity analysis**: Even worst-case (10% selectivity, 50% safe) produces 2,407/day. The frequency constraint is not binding.

### Execution Concern

High frequency introduces a different risk: **position accumulation**. At 7,220 fills/day with 1 contract per fill, if the strategy has any directional bias, positions can grow quickly. Need:
- Position limits in RiskEngine (already exists)
- Inventory management logic (flatten before session end)
- Maximum consecutive same-direction fills constraint

The existing `RiskEngine` and `StormGuard` should handle this, but the strategy must be designed with position caps from the start.

### MC-4 Verdict: PASS

---

## Config Drift Assessment (Updated)

| Item | MC Claim | Verified Reality | Drift |
|------|----------|-----------------|-------|
| TXFD6 futures cost | ~0.49 bps RT | ~0.7 bps RT | **DRIFT**: under-estimated by ~43% |
| Futures tax rate | ~0.004%/side | 0.002%/side (TAIFEX regulatory) | **DRIFT**: MC-1 double-counted sides |
| Platform fee config | Matches regulatory | `tax_rate_bps: 2.0` (wrong, should be ~0.2) | **BUG**: config has stock tax, not futures |
| TXFD6 symbol_map | Mapped | NOT mapped in fee calculator | **BUG**: FeeCalculator returns zero for TXFD6 |
| FeatureEngine | v1 (16 features) | v1 (16 features) | OK (MC uses v1 correctly) |
| Latency profile | 36ms P95 submit | 36ms P95 submit | OK |
| Fill rate assumption | 88.6% | Plausible from data | OK (historical, no adversarial adjustment) |

### Platform Bugs Found

1. **FeeCalculator symbol resolution**: TXFD6 is not in `symbol_map` (`config/base/fees/futures.yaml` line 34-41). Only TXF/TXFL5/TXFR1/MXF/MXFR1/XMT are mapped. TXFD6 returns zero fees. **Must add TXFD6 -> TX mapping**.

2. **TX tax_rate_bps**: Config says 2.0 bps but TAIFEX index futures tax is ~0.2 bps (0.00002 per side, sell only). **Must verify and correct**. If 2.0 bps is intentional (capturing some other cost), it should be documented.

---

## Overall Verdict: CONDITIONAL APPROVE

The MC-1~4 validations collectively demonstrate that the imbalance reversal strategy on TXFD6 is feasible from a platform execution perspective. The key findings:

1. **Cost barrier is broken**: Futures costs (~0.7 bps) are 5x lower than the stock cost model (3.5 bps) that originally made the strategy look impossible. This is the single most important finding.

2. **Queue structure is favorable**: Thin queues (median 1 contract) + 130ms inter-change time + 36ms RTT = high back-of-queue fill rate. This resolves the critical unknown from Stage 1.

3. **Frequency is not a constraint**: Thousands of opportunities per day, even with aggressive filtering.

4. **OFI regime signal is weak but usable**: As a feature input, not standalone.

### Conditions for Stage 2 Approval

1. **Fix FeeCalculator bugs** before any backtest that uses fee computation:
   - Add TXFD6 -> TX to `symbol_map` in `config/base/fees/futures.yaml`
   - Verify TX `tax_rate_bps` against actual TAIFEX regulatory rate

2. **Use corrected cost model** (~0.7 bps RT, not ~0.49 bps) in all Stage 2 analysis

3. **Jan/Feb data exclusion is justified but reveals data sufficiency gap** (see Addendum below)

4. **Break-even accuracy should use corrected figures**: ~63-79% depending on horizon (not 58-70% as MC-1 claimed)

---

## Addendum: Deep-Dive Checks (2026-03-26, per team-lead request)

### A1. L1 Data Field Interpretation

Verified by reading the actual numpy dtype from `research/data/raw/txfd6/TXFD6_2026-03-19_l1.npy`:

**Dtype**: `[('bid_px', '<f8'), ('ask_px', '<f8'), ('bid_qty', '<f8'), ('ask_qty', '<f8'), ('mid_price', '<f8'), ('spread_bps', '<f8'), ('volume', '<f8'), ('local_ts', '<i8')]`

- `bid_qty`/`ask_qty` represent **L1 best-level-only queue depth** (in contracts), not aggregate depth across levels.
- Distribution: median=2, 31.5% at 1, 98.1% at <=5, max=101. Consistent with TAIFEX BidAsk callback reporting best-level queue size.
- **Impact on MC-2**: The fill rate analysis is based on correct L1 queue interpretation. MC-2 findings are valid.

### A2. Jan/Feb Data Exclusion: Root Cause Identified

**Not a data encoding issue. Not pre-market artifacts. Root cause: CONTRACT MONTH MISMATCH.**

- TXFD6 = TAIEX futures, **April 2026** expiry (month code D, year 6).
- In January/February 2026, the front-month contract was TXFA6 or TXFB6 (Jan/Feb expiry). TXFD6 was a **far-month contract** with minimal liquidity.
- Far-month spread: median 232 pts (Jan 28), P90=3,121 pts. Only 0.0% of ticks have spread < 10 pts.
- In March 2026, after the March contract rolls off, TXFD6 becomes the **front-month contract** with full liquidity.
- Front-month spread: median 4 pts (Mar 19), 94.4% of ticks have spread <= 5 pts.

**Conclusion**: The Jan/Feb data exclusion is correct and necessary. The data represents a fundamentally different instrument (far-month with no liquidity). However, this means:

- **Usable front-month data: 4 trading NIGHTS only** (Mar 19-24 night sessions)
- **Not 4 days**: The March data only covers the **night session** (15:00-05:00). Day session (08:45-13:45) is NOT captured.
- **1,779,257 ticks across 4 nights** is what the MC validation is based on.

**DATA SUFFICIENCY RISK**: 4 nights of single-session data is extremely thin for strategy validation. All MC findings (queue depth, fill rate, reversal frequency, OFI IC) are conditioned on night session behavior. Day session may have different:
- Liquidity (institutional vs retail mix differs by session)
- Queue depths (potentially deeper during day session)
- Reversal rates (different volatility patterns)
- OFI dynamics

**Recommendation**: Collect day session data for TXFD6 (or the new front-month after April rollover) before Stage 2 backtesting. At minimum, need 2-4 weeks of data covering BOTH sessions.

### A3. Feature Implementation Cost Estimate

| Feature | LOC (est) | Hot-path safe? | Within 250us? | Notes |
|---------|-----------|---------------|---------------|-------|
| Return autocovariance (5s) | ~60 | Yes, with pre-allocated circular buffer | Yes (~2us) | Needs ring buffer of ~40 mid_price_x2 values (5s / 125ms tick). O(N) sum on update but N~40 is small. |
| Top-of-book survival time | ~30 | Yes, single scalar state | Yes (<1us) | Track last best_bid/ask change timestamp. Delta = now - last_change. Two int64 fields in kernel state. |
| Sharp price drop (100ms) | ~40 | Yes, with 1-slot lag buffer | Yes (<1us) | Store mid_price_x2 from ~100ms ago (1 tick lag). Compute delta. Single int64 state. |
| **Total** | **~130** | All safe | All within budget | Can be added to `_LobKernelState` with 3-5 new fields. |

All three features are lightweight stateful computations that fit naturally into the existing `_compute_values()` path in `FeatureEngine`. No allocation on hot path. Combined latency impact: <5us (well within the 250us budget).

### A4. Candidate #2 Re-evaluation with Corrected Costs

With futures costs at ~0.7 bps RT (corrected from my earlier assumption of 3.5 bps):

- Best quintile OFI IC = 0.076 on 10s horizon
- Median 10s move = 5.5 pts = 1.66 bps
- Expected edge = 0.076 * 1.66 bps = **0.126 bps per signal**
- Cost = **0.7 bps per RT**
- **Net: -0.574 bps per RT** -- still unprofitable by 4.5x

Even in the best volatility quintile, OFI alone cannot cover costs. The IC would need to be ~0.42 (= 0.7 / 1.66) to break even, which is unrealistic for any single microstructure feature.

**Candidate #2 as standalone: REJECT remains correct** even at corrected costs.

**Candidate #2 as feature input to #1**: Still APPROVE. The 0.076 IC adds incremental predictive power to the reversal classifier even if it cannot stand alone.

### A5. Updated Overall Assessment

The MC validations are methodologically sound but have a critical data limitation:

1. **All results are night-session-only** (15:00-05:00 TAIFEX). Day session behavior is unknown.
2. **Only 4 nights of front-month data** -- insufficient for robust out-of-sample validation.
3. **Cost model direction is correct** (futures 5x cheaper than stocks) but exact figures need ~43% upward correction.

**Revised verdict: CONDITIONAL APPROVE** -- proceed to Stage 2 prototype but:
- Prioritize day+night session data collection
- Use corrected 0.7 bps RT cost model
- Design Stage 2 backtest to explicitly test session-dependence of results
- Fix FeeCalculator symbol mapping before any cost-aware backtest

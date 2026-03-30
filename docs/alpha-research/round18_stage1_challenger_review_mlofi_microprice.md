# Stage 1 Challenger Review: MLOFI Micro-Price Adjustment

**Reviewer**: Challenger
**Date**: 2026-03-27
**Artifact**: `docs/alpha-research/round18_stage1_mlofi_microprice.md`

---

## Challenge 1: Data Sufficiency -- 2330 L5 Row Count is Overstated, and 10-Day OLS is Unreliable

**Claim challenged**: "2330 (TSMC): L5 depth available, ideal test case. 2.17M ticks, 11 days" (Section 2, Candidate A, line 131) and "11 days is sufficient for a 2-parameter linear model" (Section 4, line 283).

**Why it may be wrong**:

The 2.17M figure is **wrong for 2330**. The actual L5 metadata (`research/data/l5/2330_l5.npy.meta.json`) shows **537,470 rows across 10 days** (not 11). The 2.17M figure belongs to TXFD6 L5. This is a factual error that inflates the apparent data richness of the primary test case by 4x.

More substantively, 10 days is dangerously thin for fitting even a 2-parameter model (alpha + lambda) with out-of-sample validation. The standard approach would be leave-one-day-out cross-validation, giving 10 folds of 9-day train / 1-day test. But:

1. **Regime non-stationarity**: The 10 days span Feb 6 to Mar 23 -- nearly 7 weeks with potential regime shifts (volatility, volume, spread distribution changes). OLS coefficients fitted on Feb data may not predict Mar behavior.
2. **Effective degrees of freedom**: While 537K ticks seems large, MLOFI measurements at consecutive ticks are highly autocorrelated. The effective independent sample size is the number of *price change events*, which is far smaller. For 2330 at ~54K ticks/day, with a tick-to-mid-change ratio typically around 5-15%, effective N is ~5K-8K per day, or ~50K-80K total. Still seemingly large, but the regression is predicting the *residual* after L1 microprice, which is much noisier.
3. **OLS stability benchmark**: Xu et al. (2019) used 6 months of Nasdaq data for MLOFI calibration. Even with a simpler 2-parameter model, 10 days is an order of magnitude less than literature benchmarks for this class of model.

**Data needed to resolve**:
- Correct the 2330 row count to 537K (10 days).
- Report coefficient stability: fit alpha and lambda on each of the 10 individual days. If the coefficient of variation (CV) of alpha across days exceeds 50%, the model is unstable.
- Compute effective sample size using the Newey-West lag truncation from Round 11 to estimate autocorrelation-adjusted N.
- Minimum: demonstrate that the 10-fold leave-one-day-out OOS R-squared is positive on at least 8/10 days.

**Verdict**: OPEN

---

## Challenge 2: TXFD6-to-TMFD6 Transfer Assumption is Structurally Unsound

**Claim challenged**: "TXFD6 L5 MLOFI could adjust the fair value for TMFD6 quoting, since they track the same TAIEX index" (Section 3, line 260-261).

**Why it may be wrong**:

TXFD6 and TMFD6 are fundamentally different contracts despite tracking the same index:

| Property | TXFD6 (TX) | TMFD6 (XMT) |
|----------|-----------|-------------|
| Point value | 200 NTD | 10 NTD |
| Contract notional (~21000) | ~4.2M NTD | ~210K NTD |
| Participant profile | Institutional, foreign | Retail, small prop |
| Typical volume | 100K+ contracts/day | 20K-50K contracts/day |
| RT cost (NTD) | ~130 NTD | ~40 NTD |
| RT cost (bps) | ~3 bps | ~19 bps |

The critical issue is **participant profile**. MLOFI regression coefficients capture the *information content* of depth at each level, which is a function of **who places orders at those levels**. On TXFD6, L3-L5 is populated by institutional hedgers and algorithmic market makers. On TMFD6, the order book is dominated by retail traders with fundamentally different information sets and behavioral patterns.

The MLOFI beta vector from TXFD6 encodes "institutional deep-book behavior predicts price movement with weight beta_k at level k." Applying these coefficients to TMFD6 assumes retail deep-book behavior has the same predictive structure -- there is no reason to expect this.

Furthermore, the transfer path requires real-time TXFD6 L5 data to generate the MLOFI signal, then applying it as a fair-value correction for TMFD6 quoting. This introduces:
- Cross-subscription latency (two symbol feeds must be synchronized)
- Asynchronous update risk (TXFD6 L5 update arrives but TMFD6 quote has already been hit)
- The additional complexity of maintaining a second feed subscription with no fallback if TXFD6 feed drops

**Data needed to resolve**:
- Empirically measure the correlation between TXFD6 L5 MLOFI and TMFD6 mid-price changes at the 1s, 5s, and 30s horizons. If the cross-asset IC is below 0.02 at 30s, the transfer is not viable.
- Compare the L5 depth distribution (queue sizes at each level) between TXFD6 and a hypothetical TMFD6 L5 (if obtainable) to quantify the structural difference.
- If the transfer path is pursued, demonstrate that the cross-asset signal adds incremental IC beyond what TMFD6 L1 OFI already provides.

**Verdict**: OPEN

---

## Challenge 3: IC Decay from 125ms to 30s Makes the 0.03 Kill Gate Unrealistically Optimistic

**Claim challenged**: "IC gate: MLOFI correction must show IC > 0.03 at 30-second horizon on 2330 L5" (Section 5, Kill Gates, line 327) and "Signal half-life: estimated 1-5 seconds based on MLOFI gradient IC persistence" (Section 5, line 307).

**Why it may be wrong**:

Round 11 measured IC = -0.105 on 2330 at ~125ms tick cadence (approximately the median inter-tick time). This is the IC for *raw MLOFI gradient* predicting *next-tick mid-price direction*.

The IC at horizon T decays with the square root of the horizon ratio for uncorrelated signals, and faster for mean-reverting microstructure signals. The standard model is:

```
IC(T) ~ IC(t0) * sqrt(t0 / T)  [random walk]
IC(T) ~ IC(t0) * (t0 / T)^alpha, alpha in [0.5, 1.5]  [mean-reverting]
```

For the optimistic random-walk case:
- IC(125ms) = 0.105
- IC(30s) = 0.105 * sqrt(0.125 / 30) = 0.105 * 0.0645 = **0.0068**

For the realistic mean-reverting case (alpha = 1.0):
- IC(30s) = 0.105 * (0.125 / 30) = 0.105 * 0.00417 = **0.00044**

Even the most optimistic decay model gives IC(30s) = 0.007, which is **4x below the 0.03 kill gate**. The researcher's estimate of 1-5 second half-life would give IC(5s) ~ 0.105 * sqrt(0.125/5) = 0.017, which itself is below 0.03 at the *half-life* horizon, let alone at 30s.

The 0.03 threshold at 30s is actually a breakeven calculation from Section 2 ("Need IC > 0.03 at 30-second horizon to break even"). If the signal cannot plausibly reach this level, the entire Candidate A direction fails the fee breakeven test on its own stated terms.

**Important caveat**: The above uses the Round 11 *tick-cadence* IC. If the MLOFI *correction* (residual after L1 microprice) has a different autocorrelation structure -- specifically, if it captures slower-decaying information from L2-L5 -- the decay could be gentler. Xu et al. (2019) show that multi-level OFI has somewhat longer persistence than L1 alone. But the burden of proof is on the researcher to demonstrate this.

**Data needed to resolve**:
- Compute IC(MLOFI_correction, return_T) for T in {250ms, 500ms, 1s, 2s, 5s, 10s, 30s, 60s} on 2330 L5 data. Plot the IC decay curve.
- Separately compute IC for the *incremental* correction (multi-level residual after removing L1 microprice contribution) at the same horizons.
- If IC(30s) < 0.02, either (a) lower the kill gate with a justification for how sub-0.03 IC is still useful for MM fair value, or (b) abandon the 30s horizon and target a shorter horizon where the signal is above breakeven.

**Verdict**: OPEN

---

## Challenge 4: The Micro-Price Framing Does Not Escape Gate C -- It Redefines the Metric Without Resolving the Economics

**Claim challenged**: "The value is in REDUCING adverse selection (fewer fills on wrong side), not in generating standalone returns. Breakeven: improve fill quality by > 0.5 pts per fill on average." (Section 2, Candidate A, lines 136-138)

**Why it may be wrong**:

The researcher frames MLOFI correction as a "micro-price adjustment for MM" rather than a standalone directional alpha, explicitly noting that the standalone alpha failed Gate C in Round 11 (fees >> edge). The argument is that improving fair-value estimation reduces adverse selection in MM quoting, which avoids fills on the wrong side.

But this framing is economically equivalent to a directional alpha in the following precise sense:

1. **Fair-value shift = direction bet**: If the adjusted micro-price is above the current mid-price, the MM will place the bid closer to the mid (tighter) and the ask further from the mid (wider). This is functionally identical to having a long bias -- more aggressive on the bid, more defensive on the ask. The P&L impact of this adjustment is `IC * volatility * position_size`, the same formula as any directional signal.

2. **The failed Gate C is not escaped**: Round 11 showed MLOFI gradient has IC = -0.105 at tick cadence but fees overwhelm the edge at any executable horizon. Rebranding the same signal as "micro-price correction" does not change the fee structure. The 58.5 bps RT cost on 2330 is unchanged. The 4 pts RT cost on futures is unchanged.

3. **The "0.5 pts per fill improvement" target is unmeasurable in practice**: How do you measure counterfactual fill quality? You would need to compare fills *with* the correction vs *without* it, ideally in the same market conditions. This requires either (a) A/B testing in live markets (splitting order flow), or (b) a backtest simulator with realistic queue priority modeling. Neither exists in the current platform. Without a concrete measurement methodology, the value proposition is unfalsifiable.

The ONE scenario where the micro-price framing genuinely adds value beyond standalone alpha is: if the correction improves the *conditional* fill rate on profitable trades (fill more when correct, fill less when wrong) without changing the overall IC. This is a second-order effect that requires the correction to selectively improve execution quality, not just shift the fair value estimate.

**Data needed to resolve**:
- Define a concrete, measurable metric for "fill quality improvement" that can be computed from historical data. Proposal: backtest OpMM with and without the MLOFI correction, measuring the **average realized spread per fill** (price at fill vs mid-price 30s later).
- Demonstrate that the MLOFI correction produces a statistically significant improvement (p < 0.05) in realized spread per fill vs the L1-only microprice baseline.
- If the metric cannot be computed from historical data alone, explicitly state that shadow deployment is required for validation and design the measurement protocol.

**Verdict**: OPEN

---

## Challenge 5: Candidate B (Adverse Flow Quote Width) is Unexecutable at 36ms RTT

**Claim challenged**: "Candidate B: adverse_flow_quote_width -- MLOFI-driven Dynamic Quote Width" (Section 2, lines 153-194), with risk acknowledgment that "36ms RTT is the binding constraint" (line 192).

**Why it may be wrong**:

The Candidate B value proposition is: detect adverse MLOFI flow, widen quotes on the toxic side to avoid adverse fills. The researcher estimates this could avoid 5% of adverse fills (23 fills/session), saving ~46 pts/session on TXFD6.

But the timing math kills this:

1. **Detection latency**: MLOFI is computed from *changes* in the L5 book. To detect "adverse flow," you need at least 2 consecutive book updates showing the MLOFI signal building. At TXFD6's tick cadence (~125ms median), detection takes 250-500ms minimum. With EMA smoothing (which the researcher's own Round 11 optimal was EMA_output=2), add another 250ms of signal lag.

2. **Reaction chain**: Once detected:
   - Signal computation: ~1us (acknowledged)
   - Decision to modify quote: ~250us (pipeline)
   - API RTT for modify_order: **43ms P95** (from Section 5, line 304)
   - Total: ~44ms after signal is ready, ~300-550ms after adverse flow begins

3. **Adverse flow timeline**: The researcher's own Round 18 SG-LP analysis found ~50% adverse selection. For these adverse fills, the informed flow has already *filled the resting order* before the quote modification arrives. By definition, an adverse fill means the market moved through our quote -- the adverse flow was the *fill itself*, not something that preceded the fill by hundreds of milliseconds.

4. **Quantitative estimate of avoidable fills**: For quote widening to prevent an adverse fill, the modification must arrive *before* the aggressive order that fills us. Given the 300-550ms total latency from MLOFI signal detection to quote modification acknowledgment, only adverse flows that develop over >600ms AND have not yet reached our price level can be avoided. Based on TXFD6 tick cadence (125ms median, meaning price can move 1 tick per 125ms), a 600ms warning window means we can only avoid fills where the adverse move has been telegraphed across 4-5 book updates before reaching our price. This is a small minority of adverse fills -- likely 1-2%, not 5%.

5. **Savings recalculation**: 1% of 460 fills = 4.6 fills * 2 pts = 9.2 pts/session = 1,840 NTD. After accounting for reduced fill rate from wider quotes (lost spread capture on non-adverse flow), the net benefit may be negative.

**Data needed to resolve**:
- Measure the **lead time** of MLOFI signal before adverse fills in historical data. For each adverse fill (fill followed by unfavorable mid-price movement within 5s), compute how many milliseconds before the fill the MLOFI signal exceeded a detection threshold. If median lead time < 200ms, Candidate B is dead.
- Simulate the quote modification race: for each adverse fill with sufficient lead time, would a modify_order at 43ms RTT have arrived before the aggressive order?
- If fewer than 3% of adverse fills are avoidable, deprioritize Candidate B below Candidate A.

**Verdict**: OPEN

---

## Challenge 6: TXFD6 is the Real Test Asset, Not 2330 -- But the Kill Gate is Set on 2330

**Claim challenged**: "IC gate: MLOFI correction must show IC > 0.03 at 30-second horizon on 2330 L5" (Section 5, Kill Gates, line 327).

**Why it may be wrong**:

The kill gate is set for 2330 (equities), but the active MM strategies are on TMFD6/TXFD6 (futures). The researcher correctly notes TMFD6 has L1 only, and proposes using TXFD6 as proxy. But then the kill gate should be evaluated on TXFD6, not 2330, because:

1. **Different microstructure**: 2330 (TSMC equity) has different tick-to-price ratio, participant mix, and LOB dynamics than TXFD6. An IC threshold validated on equities does not guarantee transferability to futures.
2. **Different fee economics**: 2330 RT cost is 58.5 bps (massive). TXFD6 RT cost is ~3 bps (TX class: 130 NTD on ~4.2M notional). The breakeven IC is very different for each.
3. **The production path is through futures, not equities**: There is no active MM strategy on 2330. The MLOFI correction, even if validated on 2330, needs a second validation step on TXFD6 before it reaches any production strategy. The kill gate should reflect the *production-relevant* asset.

The Researcher should set the primary kill gate on TXFD6 L5 data (2.17M rows, 10 days -- adequate) and use 2330 as a *secondary* validation. This also aligns with the actual data availability: TXFD6 L5 has 4x more data than 2330 L5.

**Data needed to resolve**:
- Redefine the kill gate as: "IC > 0.03 at 30-second horizon on TXFD6 L5" (primary) AND "consistent sign on 2330 L5" (secondary).
- If IC is measured only on 2330 and not on TXFD6, the result is not promotion-relevant regardless of magnitude.

**Verdict**: OPEN

---

## Overall Assessment

**REJECT** -- with the following conditions for re-approval:

### Blocking Issues (must resolve before Stage 2 prototype):

1. **Factual correction**: 2330 L5 is 537K rows / 10 days, not 2.17M / 11 days. The 2.17M figure belongs to TXFD6.

2. **IC decay analysis** (Challenge 3): The 0.03 kill gate at 30s appears unreachable given Round 11's IC = 0.105 at 125ms and standard decay models. Researcher must either (a) present evidence of slower decay for multi-level MLOFI, or (b) revise the kill gate to a shorter horizon with updated breakeven analysis.

3. **Kill gate asset** (Challenge 6): Move primary kill gate from 2330 to TXFD6 L5, which is the production-relevant asset with 4x more data.

4. **Measurability** (Challenge 4): Define a concrete, computable metric for "fill quality improvement" before prototyping. Without this, there is no way to evaluate whether the prototype succeeds or fails.

### Non-Blocking Issues (can resolve during Stage 2):

5. **TXFD6-to-TMFD6 transfer** (Challenge 2): Flag as speculative. Do not claim TMFD6 applicability until cross-asset IC is measured.

6. **Candidate B deprioritization** (Challenge 5): Move Candidate B to "defer" alongside Candidate C. The 36ms RTT makes reactive quote widening implausible. The MLOFI signal lead time must be measured before Candidate B is prototyped.

### Recommended Stage 2 Scope (if blocking issues resolved):

Prototype Candidate A only, on TXFD6 L5 (primary) and 2330 L5 (secondary). Measure IC decay curve from 250ms to 60s. Use realized-spread-per-fill as the concrete success metric. If IC(30s) < 0.015 on TXFD6, terminate the direction.

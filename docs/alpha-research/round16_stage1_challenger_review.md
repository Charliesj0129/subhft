# Round 16 Stage 1: Challenger Review

**Date**: 2026-03-25
**Reviewer**: Challenger Agent
**Document Under Review**: `docs/alpha-research/round16_stage1_survey.md`

---

## Verdict: CONDITIONAL APPROVE

Approve with **4 mandatory conditions** that must be resolved before Stage 2 prototype begins. The survey is competent and well-sourced, but contains critical transferability gaps and an unquantified trade frequency problem that could render the combined strategy dead on arrival.

---

## Candidate #1: Imbalance Reversal Detection for Selective Maker Entries

### Challenge 1.1: Binance BTC Perpetual -> TXFD6 Transferability Gap (CRITICAL)

The primary paper (Albers et al. 2025) is tested on **Binance BTC perpetual**. This market has:
- **Zero tax** (vs. our 2.0 bps sell tax)
- **Maker rebates** of -2.5 bps (vs. our 0 bps -- we PAY to trade)
- **24/7 continuous trading** (vs. TXFD6's 5-hour session: 08:45-13:45)
- **Sub-millisecond API latency** for co-located participants (vs. our 36ms P95 RTT)
- **Massive liquidity**: BTC perpetual is the most liquid crypto instrument globally; TXFD6 has median tick interval = 125ms (3.7 ticks/sec)

The paper's reversal model exploits the negative correlation between fill probability and post-fill returns. But this relationship is **market-microstructure-dependent**:

- On Binance, maker rebates mean you get PAID -2.5 bps just for filling. A reversal trade that breaks even on price still profits from rebates. We have no such cushion.
- On Binance, the spread is sub-basis-point. On TXFD6, spread averages ~1.35 bps. The fill/return tradeoff has completely different economics at different spread levels.
- The paper itself acknowledges profits are "likely not scalable" even ON Binance. The survey's argument that "TXFD6 is far less efficient" is speculative and contradicted by our Round 14 finding that directional signal ceiling is ~0.001 bps.

**Resolution required**: Before Stage 2, quantify the minimum reversal prediction accuracy needed to overcome 2.0 bps sell tax + commission with zero maker rebates. If the required accuracy is >70%, this is likely infeasible given the paper's own reported accuracy range.

### Challenge 1.2: Queue Position is THE Bottleneck -- Not Addressed (CRITICAL)

The survey acknowledges queue position risk in one bullet ("back-of-queue maker orders are unprofitable even in reversals") but does not engage with the severity of this constraint.

Round 13 established that **queue-back adverse selection is the structural reason MM fails on TXFD6 at 36ms RTT**. Every fill is by definition the market moving against the quote (adverse price movement ~2.8 pts > spread capture ~2 pts).

The Albers paper's reversal model requires posting limit orders BEFORE the reversal materializes. With 36ms RTT:
- We are ~1 tick late (median tick interval = 125ms, so 36ms = 0.29 ticks)
- On a book with, say, 20 contracts resting at best bid, we join at position 21+
- For a reversal trade to fill, the price must move through our level -- which means the reversal has ALREADY happened, and we're now holding a position in the new (post-reversal) direction with adverse selection risk from the NEXT move

The paper's live experiment used a co-located Binance setup with sub-ms latency. They could post at or near front-of-queue. We cannot.

**Resolution required**: Estimate typical queue depth at TXFD6 best bid/ask during reversal setup conditions. If queue depth > 10 contracts and our position is always back-of-queue, the fill probability conditional on reversal is near zero (the reversal resolves before reaching our position).

### Challenge 1.3: "Selective Maker" Claim Needs Quantification

The survey claims this avoids Round 13's MM structural problems because entries are "selective." But how selective?

- TXFD6 imbalance predicts next price ~55-60% of the time. Reversals are the 40-45% minority.
- A reversal classifier must further filter these to high-confidence reversals. If the classifier has 60% precision on the 40% reversal base rate, that's 24% of all ticks qualifying.
- If we further require "safe" windows (Candidate #3), we might be down to 10-15% of trading time.
- At 3.7 ticks/sec over a 5-hour session, that's ~66,600 ticks. 10-15% = 6,600-10,000 qualifying moments.
- But we also need our limit order to actually FILL during these moments...

This matters because the paper's PnL per trade is tiny. If we're getting 5-20 fills per day (realistic with back-of-queue + selective windows), is the expected daily PnL even above 0 after transaction costs?

---

## Candidate #2: OFI Regime-Conditional Signal with Horizon Selection

### Challenge 2.1: CSI 300 != TXFD6 -- Liquidity Gap Understated

The survey claims "CSI 300 Index Futures are structurally similar to TXFD6." This is misleading:
- CSI 300 futures (IF contract) trades ~200,000 contracts/day with notional ~500B CNY
- TXFD6 trades ~80,000-120,000 contracts/day with notional ~40B NTD (~8B CNY)
- CSI 300 has institutional market makers with designated obligations; TXFD6 does not
- CSI 300 has 4.5 hour continuous session; TXFD6 has 5 hours but with pre-open auction dynamics

The Hu & Zhang paper's OU process calibration depends on the specific liquidity and participant structure. The mean-reversion timescale of OFI on a 25x more liquid market will be fundamentally different.

### Challenge 2.2: "Regime-Conditional" is a Repackaging of Round 14 R2-3 (HMM)

Round 14 rejected R2-3 (HMM Regime Detection) because:
- ~100 effective regime transitions for 12-16 parameters -- massive overfitting risk
- Cost gap unfixable by regime selection

The survey's Candidate #2 proposes essentially the same thing: detect regimes (via HMM or volatility thresholds), then trade OFI only in favorable regimes. The Round 14 finding was that even if you perfectly identify regimes, the within-regime signal is still too weak to overcome costs.

The survey's counter-argument is that "regime-conditional OFI may unlock pockets where the signal is 10-100x stronger." This is pure speculation with no supporting evidence. Our Round 14 empirical autocorrelation analysis across 6.37M ticks found the ceiling at ~0.001 bps UNCONDITIONALLY. Unless the researcher can show specific regime windows where OFI IC exceeds 0.05 (vs. unconditional ~0.01), this is a repackaged failure.

**Resolution required**: Run a quick-and-dirty test: split existing TXFD6 data into high-vol/low-vol regimes and compute OFI IC separately. If the difference is <2x, this candidate should be dropped.

---

## Candidate #3: Toxic Flow Detection for Adverse Selection Avoidance

### Challenge 3.1: Broker-Centric Model vs. Retail Taker Reality (CRITICAL)

The Cartea & Sanchez-Betancourt paper models an **internalizing broker** who receives client order flow and decides whether to internalize or hedge. The four state variables are:
1. **Own inventory** -- broker's warehouse position
2. **Informed trader inventory** -- the informed client's known position
3. **Informed volume** -- total volume from identified informed clients
4. **Uninformed volume** -- total volume from retail/uninformed clients

We are NOT a broker. We are a retail participant submitting limit orders to TAIFEX's central LOB. We do not:
- Receive identifiable client flow
- Know who the counterparty to our fill is
- Have an "informed trader inventory" signal
- Distinguish "informed volume" from "uninformed volume" at the individual order level

The survey acknowledges this ("we classify incoming taker flow as informed/uninformed based on markout analysis, not client identity") but handwaves the adaptation. Markout analysis tells you AFTER THE FACT whether flow was toxic. The paper's edge comes from classifying flow IN REAL TIME using client identity.

The Barzykin et al. result (partial information gap only 0.01%) is for a broker with PARTIAL client information, not for an exchange participant with ZERO client information. The gap between partial and zero may be much larger.

**Resolution required**: Define precisely how the four state variables translate to our context. If "informed trader inventory" cannot be observed or proxied in real-time from TXFD6 market data, state variable #2 drops out and the linear model loses a degree of freedom that may be load-bearing.

### Challenge 3.2: Filter Aggressiveness vs. Trade Volume Tradeoff

If the toxic flow filter works well, it blocks trading during informed-flow periods. But informed flow on TXFD6 may be:
- Concentrated around macro events (which happen at specific times, not uniformly)
- Correlated with volatility (which is when spread-based strategies have the best edge)

There's a real risk that the filter blocks exactly the moments when our reversal detector (Candidate #1) has the strongest signal, because reversals are more common after sharp moves, which are caused by informed flow.

---

## Combined #1 + #3 Recommendation: Trade Frequency Concern (CRITICAL)

### Challenge C.1: Expected Trades Per Day

The survey recommends implementing #1 + #3 together. Let me estimate the combined trade frequency:

**Filter stack:**
1. Toxic flow filter (#3) removes ~30-50% of trading time (informed flow periods)
2. Reversal prediction (#1) fires on ~40% of remaining ticks (reversal base rate)
3. High-confidence classifier reduces to ~60% of reversals = 24% of filtered ticks
4. Limit order must fill = depends on queue position, but optimistically 10-30% fill rate at back-of-queue

**Calculation:**
- 5-hour session = 18,000 seconds
- ~66,600 ticks (at 3.7 ticks/sec)
- After toxic filter (50% pass): 33,300 qualifying ticks
- After reversal classifier (24%): 8,000 entry signals
- After fill probability (20%): **~1,600 fills per day** (OPTIMISTIC upper bound)

But wait -- each "entry signal" requires posting a limit order and waiting for fill. If the signal window is 5-30 seconds, and we can only have one active order at a time, the actual opportunity count is:
- 18,000 seconds / 15 seconds average signal window = 1,200 non-overlapping windows
- After toxic filter: 600
- After reversal classifier: 144
- After fill probability: **~29 fills per day** (REALISTIC estimate)

At 29 fills/day with ~0.5-1.0 bps net edge per fill (generous, given queue position drag):
- Daily PnL = 29 * 0.75 bps * ~200 NTD/pt * 1 contract = ~43 NTD/day = **essentially zero**

**Resolution required**: The researcher must provide a concrete expected-trade-frequency model BEFORE Stage 2 begins. If realistic fill count is <50/day, the combined strategy may not generate enough PnL to justify the engineering cost.

---

## Rejected Papers Review

### Challenge R.1: Missing Relevant Papers?

The survey covered 60+ papers but I note some gaps:
- No papers on **TAIFEX-specific microstructure** or **Asian futures markets** (the CSI 300 paper is the closest). Were there any TAIFEX/TWSE-specific papers in q-fin.TR?
- No papers on **adverse selection measurement for exchange participants** (as distinct from brokers). The Easley-Kiefer-O'Hara PIN/VPIN family is more applicable to our context than the Cartea broker model.
- The "Red Queen's Trap" paper (2512.15732) confirms "mathematical impossibility of overcoming microstructure friction without order-flow data." This is cited as supporting but actually undermines Candidate #1, which IS an order-flow approach but faces the queue-position constraint that makes order flow data non-actionable.

### Challenge R.2: Neural HMM Rejection Too Hasty?

The Neural HMM paper (2603.20456) was rejected partly because it "predicts 500ms forward mid-price which is below our 36ms RTT resolution." This is wrong -- 500ms prediction horizon is ABOVE our RTT. A 500ms forward prediction gives us 500ms - 36ms = 464ms to act. The actual concern should be whether the 500ms horizon gives enough edge to cover costs, not whether it's below RTT.

---

## Summary of Mandatory Conditions for Stage 2

| # | Condition | Applies To | Type |
|---|-----------|------------|------|
| MC-1 | Quantify minimum reversal prediction accuracy to overcome 2.0 bps tax + commission with zero rebates | Candidate #1 | Feasibility gate |
| MC-2 | Estimate queue depth at TXFD6 best bid/ask during reversal conditions; assess fill probability at back-of-queue | Candidate #1 | Feasibility gate |
| MC-3 | Quick test: OFI IC in high-vol vs. low-vol regimes. If <2x difference, drop Candidate #2 | Candidate #2 | Kill gate |
| MC-4 | Concrete trade frequency model for combined #1+#3: expected fills/day, expected PnL/day | Combined | Feasibility gate |

**Secondary recommendations (not blocking):**
- Revisit Neural HMM rejection rationale (the 500ms horizon objection is incorrect)
- Consider PIN/VPIN literature as alternative to Cartea's broker-centric toxic flow model
- Define the four state variables for Candidate #3 in our context explicitly

---

## Verdict Rationale

CONDITIONAL APPROVE because:

1. **The survey quality is good.** Paper selection is strong (live trading data in primary paper for #1, closed-form solution in #3). Rejected papers are mostly fair rejections.

2. **The directional thesis is sound.** Moving from "predict direction" (Rounds 12-14, all failed) to "predict reversal timing + avoid adverse selection" is the right pivot. The literature supports this framing.

3. **But execution feasibility is unproven.** The four mandatory conditions address genuine showstoppers. Queue position (MC-2) and trade frequency (MC-4) are the most likely to kill the strategy. If the researcher can demonstrate >50 fills/day with positive expected PnL per fill after costs, Stage 2 should proceed.

4. **Candidate #2 is the weakest link.** It's a regime-conditional OFI strategy, which is structurally similar to Round 14's rejected HMM approach. Quick empirical test (MC-3) should determine if it survives.

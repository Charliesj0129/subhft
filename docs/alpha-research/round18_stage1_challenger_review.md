# R18 Stage 1 Challenger Review

**Date**: 2026-03-26
**Reviewer**: Claude (Challenger Agent)
**Artifact**: `docs/alpha-research/round18_stage1_survey.md`
**Status**: Stage 1 Survey — 3 candidates (RCM, SG-LP, IBH)

---

## VERDICT: CONDITIONAL APPROVE

Conditions listed below under each challenge. All must be addressed in Stage 2 design before prototype work begins.

---

## Challenge 1: Albers Rebate Gap Invalidates Headline Edge Claim

**Claim challenged**: Direction A (RCM) cites Albers et al. +0.71 bp/roundtrip as evidence of maker profitability. The survey acknowledges the rebate issue but does not quantify the impact.

**Objection**: Albers' experiment was on Binance BTC perpetual with **+0.5 bp/leg maker rebate = +1.0 bp/roundtrip**. The reported +0.71 bp/RT is therefore **net negative before rebates**: +0.71 - 1.0 = **-0.29 bp/RT from pure spread capture + signal**. On TMFD6 with zero maker rebates, the Albers strategy would be losing money even with perfect signal transfer.

The survey frames +0.71 bp as "post-fill return improvement" and lists "Expected IC Range: +0.5 to +1.5 bp per roundtrip at moderate thresholds." This range is misleading because it implicitly includes the rebate subsidy that does not exist on TMFD6.

**Data required to resolve**:
1. Decompose Albers' +0.71 bp into: (a) spread capture component, (b) directional signal component, (c) rebate component. If (a)+(b) < 0, the entire RCM premise is undermined.
2. Re-estimate expected edge on TMFD6 using actual spread distribution (avg 19.7 pts when >= 5) with zero rebate. Show explicitly: `edge = spread_capture - adverse_selection_loss - RT_cost(4pts)`.
3. If the decomposition shows the signal alone is insufficient, RCM should be downgraded to "component of IBH" rather than a standalone direction.

**Severity**: HIGH. This is not a minor caveat -- it potentially inverts the sign of Direction A's expected P&L.

---

## Challenge 2: R16 Found Wide Spread = Adverse Selection Trap; Survey's "Benign Spread" Assumption is Unsubstantiated

**Claim challenged**: The survey argues TMFD6's 45.5% wide-spread time is "less informationally loaded" than TXFD6's 2.1% because it is "common." Direction B's entire viability depends on this assumption.

**Objection**: Frequency alone does not determine whether wide spreads are benign or adverse. R16 explicitly concluded: **"Wide spread = adverse selection trap, NOT opportunity. Spread regime = contract maturity artifact."** The survey attempts to distinguish TMFD6 from TXFD6 by arguing "smaller contract, retail-dominated" but provides zero evidence for this claim.

Consider the alternative hypothesis: TMFD6 has wide spreads 45.5% of the time precisely **because** liquidity providers have been burned by adverse selection and refuse to quote tighter. If the informed-to-uninformed ratio during wide-spread periods is high, a retail maker posting at the touch is the least-informed participant -- guaranteed adverse fills.

Furthermore, the survey states "R16 showed adverse selection on TXFD6 wide spreads, but TMFD6 is structurally different." This is an assertion, not evidence. TMFD6 and TXFD6 track the same underlying index (TAIEX). The participant mix claim (retail-dominated) needs verification -- TAIFEX does not publish participant breakdown by contract size.

**Data required to resolve**:
1. Measure adverse selection rate on TMFD6 directly: for each L1 touch fill during spread >= 5 periods, compute 1s/5s/30s post-fill mid-price move direction relative to fill side. Report the percentage of fills that experience adverse mid-price movement.
2. Measure whether spread widening on TMFD6 is correlated with volatility spikes (information-driven) or time-of-day effects (structural). If the former dominates, the "benign" assumption fails.
3. Compare adverse selection rate in spread = 5 vs spread = 10 vs spread >= 20 buckets. If adverse selection increases with spread width, wider spread does NOT mean more edge.

**Severity**: CRITICAL. This is the single most important empirical question for the entire R18 round. If wide-spread periods are adverse-selection-loaded, all three directions fail.

---

## Challenge 3: Fill Rate Realism at 1.8 Ticks/Sec with 4.1 Lots Depth

**Claim challenged**: The survey assumes limit orders at the touch will fill with sufficient frequency to generate meaningful P&L. No fill rate estimate is provided.

**Objection**: At 1.8 ticks/sec and 4.1 lots average depth, a retail maker joining the back of the queue faces:
- **Queue position**: With 4.1 lots ahead (average), and assuming ~1 lot fills per trade event, a new order needs ~4 trade events to reach front. At 1.8 ticks/sec, but not all ticks are trades at the touch, realistic time-to-fill could be 30-120 seconds.
- **Stale quote risk**: During 30-120 seconds of queue waiting, price can move significantly. The 36ms RTT for cancel means ~65ms reaction time. At 1.8 ticks/sec (~550ms between ticks), this is manageable for cancellation, but the fills that DO occur are systematically the ones where price moved against you (winner's curse).
- **Opportunity cost**: If fill rate is, say, 20% of posted orders, and only 45.5% of time is eligible (spread >= 5), the strategy is active ~9% of the session. At 1 trade per fill cycle and maybe 5-10 fills per session, the N is tiny. Statistical significance requires months.

**Data required to resolve**:
1. Simulate queue position for a hypothetical 1-lot order posted at L1 touch during spread >= 5 periods. Report: (a) median time to fill, (b) fill rate (% of posted orders that fill before cancel), (c) number of fills per session.
2. Report the winner's curse metric: among simulated fills, what is the average adverse price movement at 1s/5s/30s post-fill?
3. If fills-per-session < 10, note that any backtest will have dangerously low N for statistical validation within the 58-day dataset (~580 total fills, many correlated).

**Severity**: HIGH. Low fill rate does not just reduce P&L -- it introduces massive sampling noise that makes backtest results unreliable.

---

## Challenge 4: Differentiation from R13 is Weaker Than Claimed

**Claim challenged**: The survey presents R18 as "structurally different" from R12-R17 because it uses passive maker orders instead of aggressive taker orders.

**Objection**: R13 already tested market making on TXFD6 and found it "structurally unprofitable at 36ms RTT (queue-back adverse selection)." The survey argues TMFD6 is different because of wider spreads and lower queue priority competition (RTT/tick = 7% vs 29%). However:

1. **Same broker RTT**: 36ms place, 43ms modify, 47ms cancel. The absolute latency has not changed.
2. **R13's core finding was adverse selection, not latency**: "P2-lite selective IS +3.80 but OOS FAIL." Even selective market making failed OOS. R18's "selective" approach (spread gate + reversal filter) is conceptually the same as R13's "P2-lite selective" -- just with different selection criteria.
3. **IBH is literally R13 with a spread gate**: Direction C adds inventory bounds and spread gate to A-S framework. R13 already explored A-S-style MM. The incremental novelty is the spread gate and the instrument (TMFD6 vs TXFD6), not the approach.

The survey should explicitly acknowledge: "R18 is R13 re-run on TMFD6 with a spread gate. The hypothesis is that TMFD6's spread economics change the outcome." This is honest and testable. The current framing oversells novelty.

**Resolution required**: Explicitly state what makes R18 different from R13 in falsifiable terms. The spread gate threshold (>= 5 pts) and instrument (TMFD6) are the real differences -- frame them as such, not as a "fundamentally different approach."

**Severity**: MEDIUM. This is about intellectual honesty, not viability. The approach may still work on TMFD6 even if it's conceptually similar to R13.

---

## Challenge 5: Kill Criteria Are Too Lenient

**Claim challenged**: Kill criteria for Stage 2 are: Kill B if adverse selection > 70%, Kill A if reversal frequency < 10%, Kill All if fill rate < 30%.

**Objection**:

1. **Kill B threshold of 70% is too generous**: DeLise (2024) documents that "majority of maker fills are adverse" -- i.e., > 50% adverse is the **baseline** for any maker. At 70% adverse with 4 pts RT cost and average spread of 19.7 pts when profitable:
   - 30% favorable fills: avg gain = 19.7 - 4 = 15.7 pts
   - 70% adverse fills: avg loss depends on severity. If average adverse move = 5 pts, loss = 5 + 4 = 9 pts (spread capture partially offsets)
   - But this ignores that adverse fills happen precisely when spread is collapsing -- the "19.7 pts capture" assumption breaks down for adverse fills.
   - A more realistic kill gate: if net P&L per fill (across all fills in spread >= 5 regime) is negative after 200 simulated fills, kill immediately.

2. **Kill A threshold of 10% reversal frequency is too low**: At 10% reversal rate and 20% true positive detection, you get ~2% of observations as actionable. Combined with 45.5% spread eligibility and the fill rate from Challenge 3, actionable trades per session could be < 1.

3. **Missing time-decay kill**: No kill criterion addresses the case where the strategy is "profitable in backtest but generates < 5 trades/day." At < 5 trades/day on a 58-day dataset, total N < 290. This is statistically meaningless for Sharpe estimation.

**Data required to resolve**:
1. Add a minimum-N kill gate: if expected fills per session < 5, strategy is not viable regardless of per-fill P&L (insufficient data to validate, insufficient P&L to justify operational complexity).
2. Tighten Kill B to adverse selection > 60% (not 70%). At 60% with realistic adverse loss modeling, the strategy is already marginal.
3. Add a net-P&L kill gate: if simulated net P&L per fill across the first 200 fills is <= 0, kill immediately without waiting for full backtest.

**Severity**: MEDIUM-HIGH. Loose kill gates waste Stage 2 effort on candidates that should be killed earlier.

---

## Assessment: Are Candidates Truly Differentiated from R12-R17?

**Partially.** The genuine differentiators are:

1. **Instrument change (TMFD6 vs TXFD6)**: This is real and meaningful. TMFD6's 45.5% wide-spread time vs TXFD6's 2.1% is a qualitative difference in market structure. This alone justifies a re-test.

2. **Maker vs taker**: This is a real strategic shift. However, R13 already tested maker strategies (on TXFD6), so the novelty is in the combination of maker + TMFD6, not in the maker approach itself.

3. **Spread gate**: Novel and directly motivated by TMFD6 data. Never tested before. This is the most genuinely new element.

**Not differentiated:**
- The A-S framework (IBH) is textbook MM theory already explored in R13
- OBI reversal detection (RCM) is a refinement of R16's imbalance reversal work (Albers was already surveyed in R16)
- The phi_8min filter was identified in R17 but adding it to a maker strategy does not constitute a new direction

**Bottom line**: R18 is best understood as "R13 re-run on TMFD6 with a spread gate." This is a legitimate hypothesis worth testing, but the survey should frame it honestly rather than claiming three independent directions. In reality, there is ONE hypothesis (passive making is viable on TMFD6 at wide spreads) and three flavors of testing it.

---

## Conditions for Approval

1. **Mandatory**: Stage 2 must lead with the adverse selection measurement (Challenge 2). Before any strategy backtest, measure and report TMFD6 adverse selection rate at spread >= 5. If > 60%, escalate to team before continuing.

2. **Mandatory**: Decompose Albers' +0.71 bp into spread/signal/rebate components (Challenge 1). If spread+signal < 0 bp, downgrade RCM from standalone direction to "optional enhancement for SG-LP."

3. **Mandatory**: Add minimum-N kill gate of >= 5 fills/session (Challenge 5). Strategies generating < 5 fills/day are not viable.

4. **Recommended**: Reframe the three directions as variants of one core hypothesis. Prioritize Direction B (SG-LP) as the simplest test of the core hypothesis. Only proceed to A and C if B shows adverse selection < 60%.

5. **Recommended**: Estimate fill rate (Challenge 3) before building any signal models. If fill rate is too low, the entire round is dead regardless of signal quality.

---

## Summary Table

| # | Challenge | Severity | Resolution |
|---|-----------|----------|------------|
| 1 | Albers rebate gap: +0.71 bp is net negative without rebates | HIGH | Decompose edge; re-estimate for TMFD6 |
| 2 | "Benign spread" assumption is unsubstantiated | CRITICAL | Measure adverse selection directly |
| 3 | Fill rate unknown; could be too low for viability | HIGH | Simulate queue position and fills |
| 4 | Differentiation from R13 overstated | MEDIUM | Reframe as R13-on-TMFD6 honestly |
| 5 | Kill criteria too lenient | MEDIUM-HIGH | Tighten thresholds, add min-N gate |

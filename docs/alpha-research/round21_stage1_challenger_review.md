# Round 21 Stage 1: Challenger Review

**Date**: 2026-03-27
**Reviewer**: Claude (Opus 4.6) -- Challenger Role
**Survey under review**: `docs/alpha-research/round21_stage1_survey.md`
**Verdict**: **REJECT** -- 4 unresolved challenges requiring data-backed responses

---

## Challenge 1: Candidate B's VPIN-LOW Threshold Reduction Contradicts Section 6.3

**Severity**: CRITICAL -- Internal contradiction in the survey itself

**Citation**: Candidate B pseudocode (lines 188-193) proposes:
```
if vpin_regime == LOW:
    threshold -= 1   # threshold = 4 pts (breakeven trading)
```

However, Section 6.3 "Fill Rate Reality" (lines 357-362) explicitly states:
> "Edge needed = RT cost (4 pts) + adverse selection (1-2 pts) = 5-6 pts minimum spread for profitable trading."
> "This SUPPORTS the current threshold of 5 pts and suggests we should NOT lower it to 4 during LOW regime"

The survey simultaneously proposes lowering the threshold to 4 pts during VPIN-LOW (Candidate B, line 193; Candidate B parameter table line 214: `vpin_low_subtractor: 1`; clamp floor line 206: `max(4, ...)`) AND argues that 4 pts is insufficient because of queue-back adverse selection (Section 6.3).

This is a direct self-contradiction. The `threshold_min = 4` (breakeven) floor means the strategy would trade at exactly breakeven during VPIN-LOW, which Section 6.3 argues is a LOSING proposition because adverse selection costs 1-2 additional pts.

**Required resolution**:
1. Remove the VPIN-LOW threshold reduction entirely (set `vpin_low_subtractor = 0`), OR
2. Provide empirical evidence that VPIN-LOW fills at spread=4 have net positive PnL after adverse selection. This requires per-regime fill-level PnL analysis, not the 60s horizon diagnostic proposed in D1.

---

## Challenge 2: Diagnostic D1 Uses Wrong PnL Horizon for Maker Fills

**Severity**: HIGH -- Invalid diagnostic design will produce misleading results

**Citation**: Section 7, Diagnostic D1 (lines 400-410):
```
For each tick where spread >= 5 pts:
  2. Record price change over next 60s (hold period)
  3. Compute: mean return per regime (LOW / ELEVATED / TOXIC)
```

This diagnostic measures the wrong thing. OpportunisticMM is a **market maker**. Its PnL comes from **bid-ask capture** (place limit order, get filled, capture spread edge), NOT from holding a position for 60 seconds. The relevant PnL metric is:

- Fill price vs. mid-price at fill time (immediate adverse selection)
- Fill price vs. exit price (when the position is unwound, which depends on inventory management, not a fixed 60s)

A 60s hold-period return measures directional alpha, which is irrelevant for a maker strategy. What matters is: when spread is wide and we post a limit order, do we get filled? And when we get filled, does the mid-price move against us (adverse selection)?

The existing OpMM code (`opportunistic_mm.py` line 236) delegates to `SimpleMarketMaker.on_stats()`, which places limit orders at best bid/ask. The PnL is realized when the order fills and the position is subsequently hedged or mean-reverts -- NOT at a fixed 60s mark.

**Required resolution**:
1. Redesign D1 to measure **adverse selection per fill**: for each tick where spread >= 5 and a hypothetical limit order would be placed, measure mid-price displacement at T+100ms, T+500ms, T+1s, T+5s after the fill.
2. Condition this adverse selection measure on VPIN regime.
3. If adverse selection during VPIN-TOXIC is significantly larger than during VPIN-LOW, then the VPIN conditioning has value for OpMM specifically.

---

## Challenge 3: Candidate A Dismissal -- Gamma Magnitude Calculation Needs Scrutiny

**Severity**: MEDIUM -- The math is correct but the parameter choice is not justified

**Citation**: Survey Appendix (lines 471-474):
```
For TMFD6 with gamma ~ 0.01, sigma ~ 0.0005 (30s), tau ~ 60s:
- gamma * sigma^2 * tau / 2 ~ 0.01 * 2.5e-7 * 60 / 2 ~ 7.5e-8
- This is negligible compared to the tick size (1 pt = 0.0001 in scaled terms)
- Conclusion: The gamma-dependent spread adjustment is sub-tick on TMFD6.
```

The arithmetic is correct: 0.01 * (0.0005)^2 * 60 / 2 = 7.5e-8. And 1 tick ~ 1/price ~ 0.0001 for TMFD6 at ~20000, so indeed 7.5e-8 << 0.0001.

However, the Researcher chose gamma = 0.01 without justification. The Avellaneda-Stoikov framework uses gamma as a CARA risk aversion parameter. In the literature:

- **P3 (Fodra & Labadie)** explicitly shows eta (analogous to gamma) can increase Sharpe by >2x when tuned. Their tested range is problem-specific.
- **P6 (Gueant 2016)** states `delta* ~ 1/k + gamma * sigma^2 * tau / 2` for "small gamma". The approximation breaks at large gamma.

What if gamma = 1.0 (100x larger)? Then: 1.0 * 2.5e-7 * 60 / 2 = 7.5e-6, still sub-tick (0.075 ticks). What about gamma = 100? Then 7.5e-4, which is ~7.5 ticks. At gamma=100, the inventory-dependent spread component becomes meaningful.

The question is: what is the economically reasonable range of gamma for TMFD6? The survey does not answer this. It uses a single point estimate (gamma=0.01) to dismiss the entire direction. A proper dismissal requires:

1. Calibrating gamma from actual TMFD6 inventory risk (e.g., max position, sigma, target Sharpe).
2. Showing that for ALL reasonable gamma values, the spread adjustment remains sub-tick.

That said, I acknowledge this challenge is MEDIUM severity because even if gamma were large enough to matter, the R16 result (ALL 1,080 configs negative on March) and the structural spread < breakeven problem remain. The gamma scaling cannot fix the economics. But the mathematical argument as presented is incomplete.

**Required resolution**:
State the valid gamma range for TMFD6 with derivation (e.g., from max position Q=5, sigma, target risk), and show the spread adjustment is sub-tick across the full range. Or simply note the R16 evidence makes the gamma argument moot regardless of parameter range.

---

## Challenge 4: Missing Deep Analysis of WHY R12 VPIN Failed -- and Why This Time Would Differ

**Severity**: HIGH -- Repeating past mistakes without understanding root cause

**Citation**: Section 6.4 (lines 366-372):
> "VPIN correlates with volume intensity, not with future adverse selection"
> "Regime transitions are lagging indicators"
> "As a FILTER (don't trade during TOXIC), VPIN may have value"
> "As a SIGNAL (trade more during LOW), VPIN is unreliable"

The survey correctly notes the R12/R19 failures but does not deeply analyze WHY VPIN failed as an MM overlay:

1. **R12 DD -30.6%**: Was this because VPIN-TOXIC triggered too late (after the adverse move already happened)? Or because VPIN-LOW was a false signal (market was actually toxic but VPIN said safe)? The survey says VPIN "lags" but does not quantify HOW MUCH it lags or in which direction the errors go.

2. **Asymmetric claim without evidence**: The survey claims VPIN is "useful for AVOIDING bad trades but NOT for SEEKING good trades" (line 372). This is a key architectural assumption for Candidate B (use VPIN only to ADD threshold during TOXIC, not to SUBTRACT during LOW). But this claim is stated without empirical backing. How do we know VPIN-TOXIC is a reliable AVOIDANCE signal when R12 showed DD -30.6% USING VPIN?

3. **Candidate B still uses VPIN-LOW**: Despite the stated asymmetry, Candidate B lowers the threshold during VPIN-LOW (Challenge 1). And Candidate C (lines 263-264) explicitly trades more aggressively during VPIN-LOW + low RV. This contradicts the survey's own warning.

**Required resolution**:
1. Analyze the R12 backtest data to decompose: (a) what fraction of losses came from VPIN-TOXIC signals arriving too late vs. (b) VPIN-LOW signals being wrong.
2. If R12 data is not available, state this explicitly and make the VPIN-TOXIC avoidance hypothesis a D1 prerequisite (must be validated before prototyping).
3. Remove VPIN-LOW aggression from ALL candidates until empirical evidence supports it. The only defensible use of VPIN in Candidate B is ADDING threshold during TOXIC (defensive only).

---

## Challenge 5: Wide-Spread Duration Diagnostic (D2) Does Not Account for Latency Correctly

**Severity**: MEDIUM -- Diagnostic may pass but strategy may still fail

**Citation**: Diagnostic D2 (lines 412-419):
```
When spread transitions from < 5 to >= 5:
  1. Measure duration of wide-spread episode (in ms)
  2. If median duration < 200ms: cannot react at 36ms, abort
```

The 200ms threshold vs. 36ms RTT comparison is necessary but insufficient. The real question is not "how long does the spread stay wide?" but rather: **what is the spread when our order ARRIVES at the exchange?**

The sequence is:
1. T=0: We observe spread >= 5 pts (data arrives at our system)
2. T=0+decision_latency (~0.25ms): We decide to quote
3. T=36ms: Our order arrives at the exchange
4. T=36ms+queue_time: Our order enters the book

Between T=0 and T=36ms, the spread may have already tightened. The D2 diagnostic measures total episode duration, but what matters is the **spread distribution at T+36ms conditional on spread >= 5 at T=0**.

If wide-spread episodes have a characteristic decay pattern (e.g., exponential decay with tau=100ms), then even though the median duration is >200ms, the spread at T+36ms may already be significantly narrower.

**Required resolution**:
Augment D2 to measure: "Given spread >= 5 pts at time T, what is the spread at T+36ms?" Report the fraction of cases where spread is still >= threshold at arrival time. This is the actionable metric for OpMM feasibility.

---

## Overall Verdict: REJECT

### Summary of Challenges

| # | Challenge | Severity | Status |
|---|-----------|----------|--------|
| 1 | VPIN-LOW threshold contradicts Section 6.3 adverse selection analysis | CRITICAL | Unresolved |
| 2 | D1 diagnostic uses wrong PnL horizon (60s hold vs. maker fill) | HIGH | Unresolved |
| 3 | Candidate A gamma dismissal uses unjustified parameter (gamma=0.01) | MEDIUM | Unresolved |
| 4 | No deep R12 failure analysis; VPIN-LOW aggression contradicts stated asymmetry | HIGH | Unresolved |
| 5 | D2 wide-spread duration does not measure spread at order arrival time | MEDIUM | Unresolved |

### Conditions for Approval

1. **Resolve Challenge 1**: Either remove VPIN-LOW threshold reduction from Candidate B/C entirely, or provide fill-level PnL evidence that spread=4 trades are profitable during VPIN-LOW.

2. **Resolve Challenge 2**: Redesign D1 to measure adverse selection at fill level (mid-price displacement post-fill), not directional 60s returns.

3. **Resolve Challenge 4**: Either (a) provide R12 loss decomposition showing VPIN-TOXIC avoidance works even though VPIN-LOW seeking failed, or (b) remove ALL VPIN-LOW aggression from candidates and use VPIN only as a defensive filter (add threshold during TOXIC, never subtract during LOW).

4. **Address Challenge 3**: Either properly derive the gamma range or acknowledge the R16 evidence makes the point moot.

5. **Address Challenge 5**: Augment D2 to measure spread at T+36ms, not just total episode duration.

If Challenges 1, 2, and 4 are resolved (the three structural issues), and Challenges 3 and 5 are acknowledged with mitigations, the survey can be APPROVED for Stage 2 prototyping.

# R26 Stage 1 Challenger Re-Review: Researcher Rebuttal Assessment

**Date**: 2026-04-01
**Reviewer**: Challenger Agent
**Artifact reviewed**: `outputs/team_artifacts/alpha-research/stage1_researcher_rebuttal.md`
**Original review**: `docs/alpha-research/r26_stage1_challenger_review.md`
**Updated verdict**: **CONDITIONAL APPROVE** (2 resolved, 2 partially resolved, 1 still unresolved)

---

## Challenge 1 Re-Assessment: L2-L5 Structurally Degenerate on TMFD6

**Original severity**: CRITICAL
**Updated status**: **PARTIALLY RESOLVED**

### What the Researcher showed

- L2-L5 update rates are 28-31% of snapshots — nearly identical to L1. Not sparse.
- L1-L3 fixed-weight OOS R^2 = 0.3287 vs L1-only = 0.3055 (+2.3%).
- L4-L5 hurts OOS performance. Revised to L1-L3 only.
- Fixed exponential weights (zero free parameters for weighting) outperform fitted per-level coefficients.

### Challenger assessment

The data adequately addresses the update frequency concern. L2-L3 are NOT sparse on TMFD6 — the cascading tick structure means all levels update together. This was a genuine finding I did not anticipate.

However, the +2.3% OOS R^2 improvement is **marginal and within fold variance**. The per-fold R^2 values are [0.43, 0.12, -0.01, 0.55, 0.55] for L1-L3 vs [0.38, 0.10, 0.12, 0.49, 0.44] for L1-only. In fold 3, L1-L3 gives R^2 = -0.01 while L1-only gives 0.12 — L2-L3 actually HURTS in this fold. The improvement is driven by folds 4-5 (0.55 vs 0.49/0.44).

This fold-level inconsistency is concerning: the benefit of L2-L3 appears regime-dependent. In some market conditions, L2-L3 adds noise rather than signal. This is consistent with the R18 finding (L1-only IC=0.217 > full MLOFI IC=0.206 on 2330) — L2+ information is sometimes informative and sometimes destructive.

**Residual risk**: The +2.3% improvement may not survive walk-forward validation on additional data. The proposal should proceed with L1-L3 but include a mandatory Stage 2 checkpoint: if L1-L3 does not beat L1-only on a proper walk-forward test (not just expanding-window CV on 1 day), drop L2-L3 and use L1-only MLOFI.

**My earlier claim that "R18 proved L2-L5 adds nothing" was partially wrong**: R18 tested L2-L5 on equities with fitted weights. The Researcher's fixed-weight approach on TMFD6 futures is a genuinely different methodology that yields a modestly different result. Credit given.

---

## Challenge 2 Re-Assessment: Large Trade Gate Falsified by R25

**Original severity**: CRITICAL
**Updated status**: **PARTIALLY RESOLVED**

### What the Researcher showed

- R25 used single-trade volume threshold -> 41K/day. R26 uses consecutive same-direction trade runs.
- Run >= 10: 930/day (44x more selective than R25).
- Run >= 5, volume >= 20: 913/day.
- Sweep is a gate/filter, not the signal itself.

### Challenger assessment

The Researcher correctly identifies a methodological distinction: R25 detected large individual trades, R26 detects temporal clustering of directional flow. These are genuinely different signals. 930 events/day at run >= 10 is substantially more selective than 41K single-trade "sweeps."

However, two concerns remain:

1. **Tick rule reliability on TMFD6**: The Researcher notes `trade_direction` is all zeros in CK — direction is inferred via tick rule (uptick=buy, downtick=sell). On a 1-point tick market where spread is 1-3 points, the tick rule has known biases. A "run of 10 consecutive buys" per tick rule may be an artifact of a 1-point price drift, not a genuine sequence of buy-initiated trades. The Researcher does not validate tick-rule accuracy on TMFD6.

2. **No return analysis for runs >= 10**: R25 showed mean return = 0.01 bps for single-trade sweeps. The Researcher shows run frequency (930/day) and duration (median ~500ms for runs >= 5) but does NOT show the mean forward return after a run >= 10 event. This is the critical missing data: are these runs followed by price continuation (the thesis) or mean-reversion (R25's finding)?

**Residual risk**: The run-based gate is more principled than R25's volume threshold, but without forward return evidence, we cannot confirm it filters for informationally distinct events. Stage 2 MUST compute: mean forward return at [100ms, 500ms, 1s, 5s, 30s] after runs >= 10, compared to unconditional forward returns.

---

## Challenge 3 Re-Assessment: Parameter Count vs Data

**Original severity**: HIGH
**Updated status**: **RESOLVED**

### What the Researcher showed

- MLOFI weights: fixed from literature (exp(-0.5k)), 0 free parameters
- L1-L3 only (L4-L5 dropped based on data)
- Hawkes mu: fixed from data (mean event rate)
- Hawkes beta: fixed from literature (2.0/s, Wu et al.)
- Hawkes alpha: 1 free parameter (MLE on rolling window)
- Sweep thresholds: fixed from data (run >= 10, volume >= 10)
- Signal threshold: 1 free parameter
- **Total: 2 free parameters**

### Challenger assessment

This is a strong response. Reducing from 11 to 2 free parameters fundamentally changes the overfitting calculus. With ~900 sweep events/day across 20 trading days = ~18,000 total events, calibrating 2 parameters is well within statistical safety margins (9,000 events per parameter).

The approach of fixing parameters from literature/data rather than fitting them is sound methodology. The key risk shifts from overfitting to mis-specification (what if the literature defaults don't transfer to TMFD6?) but this is testable in Stage 2 with sensitivity analysis.

**No residual risk for this challenge.**

---

## Challenge 4 Re-Assessment: Signal Horizon Mismatch

**Original severity**: HIGH
**Updated status**: **STILL UNRESOLVED** (but pivot acknowledged)

### What the Researcher showed

- MLOFI autocorrelation is near-zero at all lags (50ms to 5s).
- Mid-price change AC: +0.026 at 1 snapshot (~28ms), essentially zero at 2+ snapshots.
- **Conceded**: No predictive signal exists. MLOFI values are independent snapshot-to-snapshot.
- **Pivoted**: Candidate C (HIBDP reactive intensity breakout) instead of Candidate A (HW-DOFI predictive).
- Claims 125ms inter-update window gives 3.5x latency margin for reactive approach.

### Challenger assessment

I give the Researcher credit for an honest concession. Near-zero MLOFI autocorrelation is a definitive result that kills any "predict MLOFI continuation" strategy. Candidates A and B are effectively dead. Only Candidate C (reactive) survives.

However, the pivot to a "reactive" approach introduces a fundamental logical problem:

1. **MLOFI predicts contemporaneous price change** (R^2 = 0.31). This means the price is already adjusting AS the MLOFI event occurs. It is not a leading indicator — it IS the price change, decomposed differently.

2. **The 125ms "window" is misleading.** The 125ms median inter-update time means the NEXT BidAsk snapshot arrives 125ms later. But the price adjustment from the CURRENT MLOFI event happens within THIS snapshot, not the next one. By the time we detect a large MLOFI value, the mid-price has already moved. The 125ms is the time until the next event, not the time to exploit the current one.

3. **Reactive latency chain**: Detect MLOFI spike (0ms) -> compute Hawkes intensity (< 1ms) -> check sweep gate (< 1ms) -> submit order (36ms RTT) -> fill (unknown). Total: ~38ms minimum. But the price moved with the MLOFI event at t=0. We are 38ms late to a contemporaneous signal.

4. **The detrended IC gate was not addressed.** The Researcher did not commit to testing detrended IC. Given that MLOFI is contemporaneous (not predictive), any IC measured at forward horizons must be detrended to confirm it's not just tracking the current-snapshot price change propagating forward.

5. **R14's 0.001 bps ceiling was not addressed.** Even with the pivot, the Researcher has not shown any evidence that the reactive approach generates returns above the 4-point RT cost. The OOS R^2 of 0.33 is explanatory power (how much variance is explained), not tradeable alpha.

**This challenge remains the fundamental viability question.** The Researcher must demonstrate in Stage 2:
- Forward return (not contemporaneous R^2) conditional on the HIBDP signal firing
- Detrended IC at signal horizons [100ms, 500ms, 1s]
- Expected PnL per signal event vs the 4-point RT cost

---

## Challenge 5 Re-Assessment: Literature Transferability and Pulido Trap

**Original severity**: HIGH
**Updated status**: **RESOLVED** (downgraded to advisory)

### What the Researcher showed

- Raw L1 OFI OOS R^2 = 0.31 on TMFD6. While lower than Cont et al.'s 65% on NYSE, it confirms OFI IS a valid contemporaneous predictor on TMFD6.
- L1-L3 fixed-weight OOS R^2 = 0.33. L2-L3 adds modest but real value.
- High fold variance (R^2 from -0.01 to 0.55) reflects regime dependence.

### Challenger assessment

The Researcher's empirical validation on TMFD6 directly resolves the transferability concern. Rather than arguing from Nasdaq/Eurex literature, they showed OFI works on the actual target instrument. This is the right approach.

The Pulido Trap concern remains theoretically valid (MMs see OFI too, with lower latency) but is now a question for Stage 2 execution modeling, not a Stage 1 blocker. If the forward-return analysis (Challenge 4) shows positive returns after accounting for the reactive latency chain, the Pulido Trap is empirically refuted on this instrument.

**Residual risk**: Fold variance (-0.01 to 0.55) is substantial. The signal may only work in specific intraday regimes (open, close). Stage 2 should characterize which regimes produce positive vs negative R^2 folds.

---

## Summary of Re-Assessment

| # | Challenge | Original | Updated | Status |
|---|-----------|----------|---------|--------|
| 1 | L2-L5 degenerate on TMFD6 | CRITICAL | PARTIALLY RESOLVED | L2-L3 update 28-31%, +2.3% OOS R^2. But fold-level inconsistency; regime-dependent benefit. |
| 2 | Large trade gate falsified by R25 | CRITICAL | PARTIALLY RESOLVED | Run-based gate (930/day) is genuinely different from R25 volume threshold. Missing: forward return analysis. |
| 3 | 11 parameters on 20 days | HIGH | RESOLVED | Reduced to 2 free parameters. Sound methodology. |
| 4 | Signal horizon mismatch | HIGH | STILL UNRESOLVED | MLOFI AC = 0 confirmed. Pivot to reactive approach acknowledged, but contemporaneous R^2 is not tradeable alpha. No forward return evidence. No detrended IC commitment. |
| 5 | Literature transferability / Pulido Trap | HIGH | RESOLVED | OFI R^2 = 0.31 on TMFD6 directly validates. Pulido Trap deferred to Stage 2 execution modeling. |

---

## Updated Verdict: CONDITIONAL APPROVE

The Researcher has made substantive progress. Two challenges are resolved, two are partially resolved with clear residual risks, and one remains the critical open question.

**The proposal may proceed to Stage 2 under these mandatory conditions:**

### Stage 2 Must-Pass Gates (any failure = immediate KILL)

1. **Forward return analysis after HIBDP signal**: Compute mean forward return at [100ms, 500ms, 1s, 5s, 30s] conditional on the Candidate C signal firing. If mean return at all horizons is < 0.5 points (half the RT cost), KILL immediately.

2. **Detrended IC gate**: All IC measurements must include detrended IC (subtract local 5-min rolling mean from forward returns). If detrended IC flips sign or < 0.01, the signal is trend contamination — KILL.

3. **Sweep forward return**: Mean forward return at [100ms, 500ms, 1s, 5s] after run >= 10 events vs unconditional. If sweep events do not show statistically different returns (p < 0.05), the gate adds no value — drop it and simplify to MLOFI + Hawkes only.

4. **Tick-rule validation**: Verify tick-rule trade classification accuracy on TMFD6 by comparing against a subset where direction can be inferred from trade-through-BBO. If accuracy < 70%, the sweep gate is unreliable.

### Stage 2 Advisory Checkpoints (failure = scope reduction, not kill)

5. **L1-L3 vs L1-only walk-forward**: If L1-L3 does not beat L1-only in proper walk-forward (separate days, not expanding window on same day), drop L2-L3 and use L1-only.

6. **Regime characterization**: Identify which intraday periods produce positive vs negative R^2 folds. Restrict signal to favorable regimes only.

7. **Hawkes beta sensitivity**: Verify that beta=2.0 (literature default) is within +/-50% of TMFD6 MLE estimate. If not, beta becomes a 3rd free parameter.

### Scope Narrowing

- **Only Candidate C (HIBDP)** proceeds. Candidates A and B are dead (zero MLOFI autocorrelation kills predictive approaches).
- **Only L1-L3** (not L1-L5).
- **Only 2 free parameters** (Hawkes alpha, signal threshold).
- The burden of proof in Stage 2 is on demonstrating positive **forward returns** (not contemporaneous R^2) after accounting for the 36ms reactive latency.

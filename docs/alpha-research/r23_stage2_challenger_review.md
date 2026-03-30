# R23 Stage 2 — Challenger Review

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Scope**: Stage 2 diagnostic results for A1, A2, C

---

## Overall Assessment: A1/A2 KILLS CONFIRMED, C CONDITIONAL APPROVE with caveats

---

## 1. Kill Decision Correctness

### A1 (Confidence-Weighted OFI): KILL CONFIRMED

No challenge. The correlation is literally +1.000 on both instruments. The diagnostic conclusively proves what my C2 challenge predicted: on BBO-inferred data, ALL trades are at-quote (100% at-quote, 0% tick-rule, 0% inside-spread), so the confidence weight is uniformly 1.0 and the signal is numerically identical to unsigned OFI.

The Researcher's note about A1 being "recoverable with real tick-level data" is fair -- once the TradeClassifier is wired into the live pipeline with actual TickEvent data (where inside-spread and tick-rule trades exist), this could be re-evaluated. But on current infrastructure, A1 is dead. **No objection.**

### A2 (Cancel-Volume OFI): KILL CONFIRMED

No challenge. Max IC = +0.009 (TXFD6 at +10s), well below the 0.015 threshold at ALL horizons on BOTH instruments. The signal is orthogonal (corr 0.12-0.22) and not fill-contaminated (35% fill fraction), but it simply does not predict returns. **No objection.**

### C (Toxicity Score): Challenges below

---

## 2. Adverse Movement Methodology Review

### C2.1: Methodology Is Generally Sound (PASS)

The quintile bucketing approach is standard and appropriate:
- Trades bucketed by toxicity EMA at trade time into 5 quintiles
- Forward mid-price movement measured at +5s, +10s, +30s, +60s
- Adverse movement correctly signed: for buys, price drops are adverse; for sells, price rises
- Session boundaries respected (no cross-session look-ahead)
- Large sample sizes: 461K valid trades (TXFD6), 1M (TMFD6)

### C2.2: Adverse Movement Bug Risk (MEDIUM CONCERN)

In `diagnostic.py:356-360`, the adverse movement computation uses `searchsorted` to find the future timestamp within the session segment. The index `fi` is session-relative (into `seg_ts/seg_mid`) while `ti = t_idx[j]` is global. However, the actual mid-price values extracted are correct because `seg_mid[fi]` gives the session-local future price and `mid[t_idx]` gives the global current price -- both are mid-price values, not indices. **No bug found after careful inspection.**

### C2.3: Horizon Selection (ADEQUATE)

The four horizons (+5s, +10s, +30s, +60s) cover the range where adverse selection is most acute. Missing +120s and +300s for completeness, but these are less relevant for fill-level adverse selection (the effect should be strongest in the first 30s).

---

## 3. TMFD6 IC (+0.045 at 10s): Real or Artifact?

This is the most important question. The IC profile for Candidate C on TMFD6 is:

| Horizon | IC |
|---------|------|
| +10s | +0.045 |
| +30s | +0.025 |
| +60s | +0.006 |
| +120s | -0.022 |
| +300s | -0.016 |

### C3.1: The IC Profile is CONSISTENT with Genuine Short-Horizon Signal (LIKELY REAL)

Arguments FOR reality:
1. **Non-monotonic**: IC peaks at +10s and decays to near-zero by +60s, then goes negative. This is characteristic of a genuine short-horizon microstructure signal, not trend contamination (which would show monotonically increasing IC).
2. **Sign reversal**: The flip to negative IC at +120s/+300s suggests the toxicity signal captures short-term momentum followed by mean-reversion -- exactly what the adverse-selection literature predicts (informed flow moves price temporarily, then partial reversion).
3. **Magnitude**: +0.045 at 10s is in the plausible range for microstructure signals (R17 TSMC lead-lag was +0.061, other feature ICs in the 0.02-0.05 range).
4. **No trend contamination**: The R18 detrended IC gate was applied (5-min block detrending). The non-monotonic profile further confirms no trend leakage.

### C3.2: HOWEVER, the TMFD6 IC Does Not Survive at Actionable Horizons (CONCERN)

The signal decays to +0.006 by 60s. Our cost structure on TMFD6 requires holding for 60s+ to be economically meaningful (R14/R16 finding). An IC of +0.006 at 60s is well below the 0.015 kill threshold. This means:

- **As a standalone directional alpha on TMFD6: DEAD.** The 10s IC is irrelevant because we cannot trade at 10s horizon profitably given the 3.92 pts RT cost.
- **As a gate signal: The 10s IC is useful** because the gate decision happens BEFORE entry, not after. High toxicity at the moment of a potential entry predicts adverse movement over the next 10-30s, which is exactly the window where a market-making fill would be most damaged.

**Verdict**: The TMFD6 IC is likely real but only useful as a gate signal (decide whether to quote/enter), not as a directional alpha (decide which direction to trade). This is consistent with the Researcher's framing.

---

## 4. 100% At-Quote Classification: Thesis Implications

### C4.1: This Invalidates BBO-Inferred Signed Flow, NOT the Signed-Flow Thesis Itself

The 100% at-quote finding is a direct consequence of the data reconstruction method, not a property of the market:

1. **BBO data infers trades from mid-price changes.** By construction, if mid goes up, the inferred trade price = previous ask (at-quote BUY). If mid goes down, inferred price = previous bid (at-quote SELL). There is no mechanism to produce inside-spread trades from BBO snapshots.

2. **Real tick data DOES contain inside-spread trades.** On TXFD6 with median spread = 1+ points, trades can and do occur inside the spread during multi-tick spread regimes. The TradeClassifier's tick-rule and inside-spread paths would be exercised with actual TickEvent data.

3. **The signed-flow thesis is untestable on BBO-only data.** The entire value proposition of trade-signing (distinguishing informed from uninformed flow) requires knowing whether a trade aggressively crossed the spread vs. passively improved inside the spread. BBO reconstruction collapses this distinction.

**Implication**: The A1/A2 kills are kills of the BBO-inferred version, not definitive kills of the signed-flow concept. Re-evaluation after TradeClassifier pipeline integration is justified. However, I would set a tight timeline (30 days of live data) to prevent this from becoming an indefinite "maybe later."

### C4.2: Candidate C Is Partially Immune to This Issue

The toxicity score (EMA of signed direction) is less affected by the at-quote collapse because:
- It uses the DIRECTION (+1/-1) of each inferred trade, not the confidence
- BBO-inferred direction is correct (mid went up = someone bought, mid went down = someone sold)
- The EMA smoothing aggregates many trades, reducing single-trade classification noise

The toxicity score's value comes from the IMBALANCE of buy vs. sell flow, not from the precision of individual trade classification. This is a robustness advantage.

---

## 5. C Promotion to FE [21] — DISAGREE WITH SLOT ASSIGNMENT

### C5.1: vrr Slot Conflict

The Researcher proposes `toxicity_ema50_x1000` at index [21], claiming "vrr slot is available since vrr was never registered." But:

1. **vrr IS computed** in `engine.py:534` (`vrr_val = self._compute_vrr(ks, mid_price_x2)`) and appended to the output tuple.
2. **vrr is NOT registered** in `registry.py` (only 21 FeatureSpec entries, indices [0]-[20]).
3. **But vrr IS the 22nd element** of the output tuple (index [21] in zero-based).

If we assign toxicity to index [21], it would conflict with the vrr value already being emitted at that tuple position. Any code consuming the feature tuple by position (not by registry lookup) would break.

**Required**: Either (a) register vrr at [21] first, then assign toxicity to [22], or (b) remove vrr from the engine output tuple, then assign toxicity to [21]. Do NOT silently overwrite a live output position.

### C5.2: Gate-Only Promotion is Appropriate

I agree that C should be promoted as a **gate/filter signal**, not a standalone alpha. The evidence supports this framing:
- TXFD6 adverse movement Q5-Q1 = +3.5 pts at +60s (economically significant for OpMM)
- Low correlation with spread (-0.06), confirming it is not a spread proxy
- TMFD6 directional IC at 10s is useful for entry timing, not for directional trading

### C5.3: OpMM Integration Timing

The Researcher proposes adding `_check_toxicity_condition()` to `opportunistic_mm.py`. Given OpMM shadow is not yet deployed with working thresholds, this integration should wait until:
1. TradeClassifier is wired into the live pipeline
2. At least 5 days of live classified data is collected
3. Toxicity thresholds are calibrated on live (not BBO-reconstructed) data

Premature integration into OpMM with BBO-calibrated thresholds risks miscalibration when real classified data arrives.

---

## Summary

| Item | Verdict | Notes |
|------|---------|-------|
| A1 kill | CONFIRMED | corr=1.000, dead on BBO data |
| A2 kill | CONFIRMED | IC < 0.015 everywhere |
| C conditional pass | APPROVE | Adverse movement is real (+3.5 pts Q5-Q1), not a spread proxy |
| TMFD6 IC +0.045 | LIKELY REAL | Non-monotonic, consistent with short-horizon adverse selection |
| 100% at-quote | Data limitation, not market property | Re-evaluate A1 after live TradeClassifier integration |
| C at FE [21] | DISAGREE — slot conflict with vrr | Fix vrr registration first |
| C OpMM integration | DEFER | Wait for live classified data, not BBO-reconstructed thresholds |

### Conditions for Stage 3 Approval

1. **Resolve vrr slot conflict** before assigning toxicity a feature index.
2. **TradeClassifier pipeline integration** is the critical-path prerequisite. Set a 30-day deadline for live data collection.
3. **Re-evaluate A1** once 5+ days of live classified data (with tick-rule and inside-spread trades) is available.
4. **Defer OpMM threshold calibration** to post-live-classification data.

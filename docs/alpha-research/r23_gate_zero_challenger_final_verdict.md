# R23 Gate Zero — Challenger Final Verdict

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Scope**: Gate Zero v2 corrected results

---

## Final Verdict: MARGINAL PASS — proceed to Stage 2 with LOW expectations and strict kill gates

---

## GZ-C1 Resolution: CONFIRMED ARTIFACT, partially real signal survives

The researcher confirmed the centered MA artifact. The correction is thorough:

| Signal | v1 (artifacted) | v2 RAW | v2 Causal | Artifact fraction |
|--------|-----------------|--------|-----------|-------------------|
| TMFD6 1h | -0.320 | -0.178 | -0.099 | ~55-70% |
| TXFD6 15min | -0.238 | -0.152 | -0.017 | ~35-93% |

The v1 "10-18x threshold" claim was approximately 50% artifact. The genuine signal is modest.

**GZ-C1: RESOLVED.** The researcher acknowledged the flaw, re-ran correctly, and reported honestly. The methodology lesson (always use causal detrending, compare against RAW) should be added to the research team's mandatory checklist.

---

## GZ-C2 Resolution: RESOLVED

v2 uses per-day IC only, no cross-day pooling. Confirmed headline numbers are per-day means.

---

## Assessment of Surviving Signal

What remains after artifact removal:

**TMFD6 1h RAW: IC = -0.178, p = 0.034**

This is the only statistically significant result. My assessment:

**In favor:**
- p = 0.034 passes the 0.05 threshold
- 72% of days show negative IC (consistent direction)
- CI excludes zero: [-0.327, -0.035]
- IC of -0.18 exceeds the 0.020 gate by 9x (though far from the v1 claim of 18x)
- The direction (mean reversion) is economically sensible for index futures

**Against:**
- Single instrument (TMFD6 only — TXFD6 does not confirm at 1h)
- Single horizon (only 1h — not 30min, not 2h, not 15min)
- Causal detrending reduces to IC = -0.099, p = 0.15 (no longer significant)
- 18 trading days is a small sample — p = 0.034 on N=18 is borderline
- Multiple comparisons: we tested 5 horizons x 2 instruments = 10 hypotheses. A Bonferroni-corrected threshold would be p < 0.005, which this fails.
- The cost viability is undemonstrated

**The causal detrending concern is the most troubling.** If the signal disappears under causal detrending (IC drops from -0.178 to -0.099, p from 0.034 to 0.15), it suggests the "mean reversion" in RAW returns is partially explained by local trend structure that would not be exploitable in real-time (where you can only use past data). A real-time strategy can only act on causal information.

---

## Verdict: MARGINAL PASS

I will not kill this signal outright, because:
1. The RAW IC of -0.178 at 1h is genuine and above threshold
2. The p-value (0.034) is significant, even if borderline
3. Index futures mean-reverting at 1h is economically plausible
4. No prior round tested this timescale, so it is genuinely novel

But I attach STRICT conditions for Stage 2:

### Stage 2 Mandatory Requirements

1. **Jan/Feb vs March split (GZ-C3, still unaddressed)**: Report TMFD6 1h IC separately for Jan/Feb wide-spread days and March tight-spread days. If IC is only significant in Jan/Feb, the signal is regime-dependent and unreliable.

2. **Multiple comparison correction**: Acknowledge that 10 hypotheses were tested. Report adjusted p-values (Bonferroni or FDR). The uncorrected p = 0.034 does not survive Bonferroni (threshold = 0.005).

3. **Causal-only strategy design**: The Stage 2 strategy must use ONLY causal (backward-looking) information for entry decisions. If causal IC = -0.099 is the true signal strength, the expected edge is roughly half the RAW estimate. Design the strategy accordingly.

4. **Minimum backtest trades**: N >= 50 non-overlapping 1h trades to reach statistical power. With overlapping windows, this is achievable within the 20-day sample, but overlapping introduces its own autocorrelation concerns.

5. **Absolute kill gates for Stage 2**:
   - Backtested mean PnL per trade < 0 (after 3.92 pts RT cost): KILL
   - Backtested Sharpe < 0.5 annualized: KILL
   - Win rate < 45%: KILL
   - Signal disappears in March-only data: KILL

---

## R23 Overall Status After Gate Zero

| Item | Status |
|------|--------|
| Candidate A (trend following) | KILLED (data shows reversion, not trending) |
| Candidate A' (mean reversion at 1h) | MARGINAL PASS — Stage 2 with strict kills |
| Candidate B (cost reduction) | LOW-MEDIUM — side investigation |
| Candidate C (calendar patterns) | Removed — passive logging only |

The "strongest signal in 23 rounds" turned out to be a methodological artifact. After correction, a modest, single-instrument, single-horizon mean-reversion signal survives. This is worth investigating but expectations should be LOW. The 50% artifact rate is a sobering reminder that extraordinary claims require extraordinary methodology.

---

## Methodology Lesson (should be institutionalized)

**Rule: Never use centered (look-ahead) detrending when measuring autocorrelation or predictive IC.** Centered MA subtraction mechanically induces negative lag-1 autocorrelation of approximately -1/(w-1) in the residuals, regardless of the true data generating process. This is a well-known result in time-series analysis but easy to forget in practice.

Add to research team checklist:
- [ ] All detrending must be causal (backward-looking only)
- [ ] Always report RAW return autocorrelation alongside detrended
- [ ] If detrended IC matches -1/(w-1), suspect artifact before claiming signal

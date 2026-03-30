# R23 Stage 1 — Challenger Final Verdict

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Scope**: Re-review of Researcher responses to C1-C5

---

## Final Verdict: APPROVE WITH CONDITIONS (Gate Zero required)

All challenges are satisfactorily resolved. The researcher demonstrated intellectual honesty by accepting corrections and downgrading candidates appropriately. However, the net outcome is sobering: no candidate has demonstrated feasibility.

---

## Challenge Resolution Assessment

### C1: Single-Contract Trend Following Literature Gap — RESOLVED

The Researcher fully accepts the challenge:
- Withdraws the unsourced "5-15 bps/trade" claim
- Acknowledges DeePM's Sharpe comes from 50-instrument diversification, not single-contract signals
- Acknowledges Safari & Schmidhuber's key caveat ("trends revert before significance")
- Downgrades Candidate A from HIGH to UNKNOWN pending Gate Zero empirical test

The proposed Gate Zero is well-specified: detrended return autocorrelation at 1h/2h/4h on TMFD6/TXFD6 ClickHouse data, IC threshold >= 0.020. This is falsifiable and appropriate.

**One concern**: The higher IC threshold (0.020 vs standard 0.015) is justified by lack of diversification, but the Researcher should also specify a MINIMUM number of observations. At 1hr granularity on TMFD6, a 20-day dataset yields ~100 non-overlapping observations per horizon. This is borderline for statistical significance. At 4hr granularity, you get ~25 observations — insufficient. The Gate Zero test must report confidence intervals, not just point IC estimates.

**Status**: RESOLVED. Candidate A proceeds to Gate Zero only.

---

### C2: vrr Registry Gap — RESOLVED

The Researcher verified the code, confirmed zero grep results, and provided a concrete fix plan (~85 LOC, 0.5-1 day). Importantly, the Researcher correctly notes that vrr registration may be moot if Candidate A fails Gate Zero.

The alternative of using `ret_autocov_5s_x1e6` [17] as a weaker regime proxy is reasonable for initial Gate Zero testing — it avoids blocking on vrr registration work.

**Status**: RESOLVED. No action needed until Gate Zero outcome is known.

---

### C3: Cost Savings Inflated — RESOLVED

The Researcher's revised cost decomposition is honest and correct:

| Component | RT Cost | Addressable? |
|-----------|---------|--------------|
| Tax (sell) | 0.66 pts | NO |
| Commission | 2.60 pts | NO |
| Spread crossing | 0.66 pts | YES |
| **Total** | **3.92 pts** | **0.66 pts** |

The fixed component (3.26 pts) is 83% of the total RT cost. The addressable spread-crossing component is only 0.66 pts. R16's 1.2 pts saving comes from avoiding spread-crossing on one side (passive entry), which captures most of the available headroom.

The Researcher correctly concludes that additional optimization beyond R16's passive entry yields only ~0.3-0.5 pts — far below the original 1.5-2.0 pts claim.

The kill conditions are now specified and measurable:
1. Fill rate < 30% at BBO
2. Adverse selection on fills > 1.5 pts at 10s
3. Spread <= 1 tick for > 80% of session

**Status**: RESOLVED. Candidate B downgraded to LOW-MEDIUM. The fill-probability optimization literature is not applicable to TMFD6's thin book.

---

### C4: Candidate C Reclassified — RESOLVED

Candidate C correctly removed from research candidate list and reclassified as passive logging task. No further action.

**Status**: RESOLVED.

---

### C5: Platform Value at MF — RESOLVED

The Researcher's answer is honest: the platform's value at MF is data infrastructure (ClickHouse + FeatureEngine + monitoring) and execution plumbing (broker + risk), not latency optimizations. The Rust ring buffers, fused normalizer, and sub-100ns classification are explicitly acknowledged as irrelevant at 30min+ horizons.

This is a pragmatic assessment. The platform DOES provide value for MF trading — just not the kind of value it was designed for.

**Status**: RESOLVED.

---

## Net Assessment

| # | Challenge | Resolution Quality | Final Status |
|---|-----------|-------------------|--------------|
| C1 | Literature gap | STRONG — full acceptance, Gate Zero specified | RESOLVED |
| C2 | vrr dead code | STRONG — code-verified, fix plan provided | RESOLVED |
| C3 | Cost inflation | STRONG — honest decomposition, revised estimate | RESOLVED |
| C4 | C is not research | ACCEPTED — reclassified | RESOLVED |
| C5 | MF platform value | ADEQUATE — honest answer | RESOLVED |

## Strategic Observation

After this review cycle, the R23 survey has been corrected from three "feasible" candidates to:
- **Candidate A**: UNKNOWN (requires Gate Zero that may fail)
- **Candidate B**: LOW-MEDIUM (marginal improvement over R16 heuristic)
- **Candidate C**: Removed (not research)

This is a significant downgrade from the survey's original optimism. The "narrow but real viable path" narrative is now contingent on a Gate Zero test that the Researcher himself cannot predict will pass. The honest assessment is:

**There is currently NO demonstrated viable alpha path for this platform.**

This is not a failure of the research process — it is the research process working correctly. 22 rounds of rigorous testing have systematically eliminated directions. The remaining question (hour-scale trend following on TMFD6) is genuinely unknown and worth testing, but the expected outcome based on prior rounds is negative.

The Researcher's strategic pivot thesis ("rich signals, patient execution") remains conceptually valid, but specific implementations remain unproven. Gate Zero is the correct next step.

---

## Approval Conditions

**APPROVE** proceeding to Gate Zero empirical test with the following requirements:

1. **Gate Zero scope**: Detrended return autocorrelation on TMFD6 and TXFD6 at 1h, 2h, 4h horizons
2. **IC threshold**: >= 0.020 (detrended, single-contract)
3. **Minimum observations**: Report confidence intervals. At 4h granularity, acknowledge N~25 is insufficient for standalone significance
4. **Regime proxy**: Use `ret_autocov_5s_x1e6` [17] (already registered) as initial regime indicator. Defer vrr registration until Gate Zero outcome
5. **Candidate B**: May proceed as a low-priority side investigation (passive entry optimization), but should not consume significant research budget given ~0.3-0.5 pts headroom
6. **Candidate C**: Passive logging only. No research allocation.
7. **If Gate Zero fails**: The honest conclusion is that this platform, under current constraints (single broker, 1-2 instruments, no maker rebates), does not have a viable alpha path. This is a valid and valuable finding.

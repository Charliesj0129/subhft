# R18 Stage 1 Challenger Review — Literature Survey Candidates

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Artifact reviewed**: `docs/alpha-research/round18_stage1_literature_survey.md`
**Status**: Complete
**Candidates**: A (TSM-CR), B (HMM-RCM), C (VRB)

---

## Overall Verdict: REJECT (A) / CONDITIONAL (B) / CONDITIONAL (C)

Only B and C should proceed to Stage 2, and only under strict conditions. Candidate A is a re-proposal of a signal already tested and partially killed in R17 -- the cubic reversion coefficient is NOT significant on TMFD6. The survey fails to cite this critical prior result.

**Corrected constraints applied throughout**:
- RT cost = 39.2 NTD = 3.92 pts = **1.19 bps** (not 1.33 bps as the survey states)
- FeatureEngine v2 = **21 features** (not 18)
- Breakeven IC at 30min = 0.037, at 1h = 0.026, at 4h = 0.013 (adjusted from survey's 0.043/0.030)

---

## Candidate A: Trend-Scaled Momentum with Cubic Reversion (TSM-CR)

### Challenge A-1: FATAL — The Cubic Reversion Model Was Already Tested on TMFD6 in R17 and FAILED

**Claim challenged**: Survey proposes fitting E(r) = b*phi + c*phi^3 on TMFD6 with rolling 20-day calibration, claiming expected IC 0.03-0.08.

**Objection**: This is a direct re-proposal of R17's Candidate 2 "Multi-Timescale Trend Reversion (MSTR)," which used the exact same Safari & Schmidhuber (2501.16772) paper, the exact same cubic model, and the exact same TMFD6 data.

R17 prototype results (`docs/alpha-research/round17_stage2_prototype.md`, `research/alphas/multiscale_trend_reversion/impl.py`):

> **"ALL cubic c coefficients NOT statistically significant (|t| < 2.0). The paper's key tradeable mechanism (reversion at phi_c) is absent on TMFD6."**

> **"TMFD6 shows momentum (positive IC) at sub-hour scales, NOT mean-reversion. The Schmidhuber 'universal' reversion does not apply to this instrument."**

R17 Final Report verdict: **"FAIL as standalone contrarian. PASS as momentum feature."** The only salvageable output was phi_8min as a momentum indicator (IC=+0.041, t=9.0), which was classified as a FeatureEngine candidate, not a standalone alpha.

The R18 survey cites Safari & Schmidhuber (2501.16772) and Schmidhuber (2020, 2006.07847) as its primary references -- the SAME papers used in R17. It proposes the SAME cubic model. It does not cite the R17 prototype results. This is a process failure: re-proposing a killed signal without acknowledging prior negative evidence.

**The survey's Signal Description even describes the two components**:
1. "Weak-trend momentum" (|phi| < threshold) -- this IS phi_8min, already a FeatureEngine feature
2. "Strong-trend reversion" (|phi| > threshold) -- this is the cubic term, already shown to be statistically absent on TMFD6

**Severity**: FATAL. The core tradeable mechanism (cubic reversion) has been empirically falsified on the target instrument. The momentum component already exists as phi_8min in FeatureEngine v2.

### Challenge A-2: "R^2 ~ 0.5-2%" is Multi-Asset Aggregated -- Single-Instrument Translation is Undefined

**Claim challenged**: Survey states "Safari & Schmidhuber report R^2 ~ 0.5-2% for cubic model on daily data aggregated across 24 assets" and extrapolates "for single-asset intraday, expect IC ~ 0.03-0.08 at 1-4h horizon."

**Objection**: The paper's 0.5-2% R^2 is computed by pooling 14 years of data across 24 diverse assets (equity indices, rates, FX, commodities). Single-instrument R^2 is typically far lower and highly variable. The extrapolation from multi-asset daily R^2 to single-asset intraday IC is unjustified. There is no formula or empirical basis provided for this translation.

Furthermore, R17 MEASURED the actual IC on TMFD6:
- phi_8min fwd_1min: IC = +0.041 (momentum, not reversion)
- phi_8min fwd_30min: IC = +0.037 (momentum, not reversion)
- phi_64min fwd_30min: IC = -0.035 (t=-0.9, NOT significant)

The actual measured ICs are positive (momentum), contradicting the survey's cubic reversion hypothesis. The only horizon where negative IC appears (phi_64min) is not statistically significant.

**Severity**: HIGH. The IC expectation is speculative and contradicted by actual measurements.

**Verdict for A: REJECT.** Re-proposal of a killed signal. The cubic reversion mechanism is absent on TMFD6. The momentum component (phi_8min) already exists in FeatureEngine v2. No new testable hypothesis is offered beyond what R17 already explored.

---

## Candidate B: HMM Regime-Conditioned Momentum (HMM-RCM)

### Challenge B-1: R17 Already Found Regime-Conditioning Does Not Overcome the Cost Barrier on TMFD6

**Claim challenged**: Survey proposes HMM with 2-3 states to identify trending vs reverting regimes, expecting IC 0.04-0.10 at 30min horizon.

**Objection**: R17 Candidate 3 (Regime-Adaptive OFI) explicitly tested regime-conditioning on TMFD6. The R17 finding was:

> **"No regime overcomes 4-pt cost barrier."**

Even the best regime-conditioned IC (multi-factor quiet regime: IC=+0.129 at fwd_1min) produced negative P&L after costs:

| Regime | IC@5min | sigma_5min | Expected edge | Net after cost |
|--------|---------|------------|---------------|----------------|
| Quiet | +0.040 | ~6 bps | 0.19 bps | **-1.14 bps** |
| Normal | +0.126 | ~8 bps | 0.80 bps | **-0.53 bps** |
| Volatile | +0.110 | ~14 bps | 1.23 bps | **-0.10 bps** |

The R17 conclusion was: "IC=0.129 (best found) still < cost barrier. Confirms R16: no L1 signal alone overcomes 4pt RT cost."

The HMM-RCM proposal uses a different regime detection method (HMM vs spread-based classification), but the underlying issue is structural: TMFD6's cost structure requires IC > ~0.037 at 30min (corrected from 0.043 with updated 3.92 pts cost) to break even. R17 showed that even the best regime-conditioned signals fall short at horizons where costs matter.

**Counter-argument the Researcher might make**: HMM-RCM operates at 5min-2h horizons (longer than R17's 1-5min), where sigma is larger and breakeven IC is lower. This is valid -- at 1h horizon, breakeven IC drops to ~0.026 (corrected). But this leads to Challenge B-2.

**Severity**: HIGH. Must demonstrate why HMM regime detection produces fundamentally different results than R17's regime conditioning.

### Challenge B-2: 58 Days / 16,700 Bars is Insufficient for Robust HMM Calibration + OOS Validation

**Claim challenged**: Survey states "58 days of 5min bars = ~16,700 bars. Sufficient for 2-state HMM (needs ~500-1000 bars for stable calibration)."

**Objection**: The 500-1000 bar claim is misleading. A 2-state Gaussian HMM has 7 free parameters (2 means, 2 variances, 2 transition probabilities, 1 initial distribution). While 500 bars may be sufficient for PARAMETER ESTIMATION, the actual requirement for a TRADING STRATEGY includes:

1. **Calibration window**: 30-day rolling (survey proposal) = ~8,600 bars for training
2. **OOS evaluation**: Remaining ~8,100 bars = 28 days
3. **Number of regime transitions**: A 2-state HMM needs sufficient transitions to estimate transition probabilities reliably. If the typical regime duration is 30-120 minutes (6-24 bars), expect ~30-100 transitions per 30-day window. This is borderline for transition matrix estimation.
4. **Walk-forward validation**: With 28 OOS days and 3-8 trades/session, total OOS trades = ~84-224. For Sharpe estimation with 84 trades, the standard error is ~0.11/sqrt(84) = ~0.11. A Sharpe of 0.5 would not be distinguishable from zero at 2-sigma.

Furthermore, the survey proposes "recalibration every day" which means the HMM parameters shift daily. With only 28 OOS days, each recalibration is tested on exactly 1 day. There is no way to assess parameter stability.

**R13 precedent**: R13's "P2-lite selective" strategy showed IS +3.80 bps OOS FAIL with a similar paradigm (parameter optimization on limited data). HMM-RCM has more parameters (7 HMM + trading rule thresholds) than P2-lite, increasing overfitting risk.

**Severity**: HIGH. Statistical validation will be inconclusive with available data. Must pre-commit to minimum-N and minimum-OOS-days requirements.

### Challenge B-3: "No Time-Lag" Claim for HMM is Misleading

**Claim challenged**: Survey states "Key advantage: no time-lag (unlike moving average crossovers), accurate regime shifts at market change points."

**Objection**: This describes the SMOOTHED (Viterbi/backward) HMM, not the FILTERED (forward-only) HMM. The survey correctly notes (Risk Factor #3) that only filtered probabilities should be used. But filtered HMM probabilities DO lag: the forward algorithm updates P(state|data_1:t) incrementally. After a regime change, the filtered probability shifts gradually over several bars, not instantaneously. The speed depends on the emission distribution overlap between states.

In practice, a 2-state HMM with overlapping Gaussian emissions (which is inevitable for financial returns) takes 3-10 bars (15-50 minutes at 5min bars) to confidently switch regime probability from 0.3 to 0.7. This IS a lag, comparable to a 30-minute EMA crossover. The "no lag" advantage is overstated.

**Severity**: MEDIUM. Does not kill the candidate, but the survey overestimates the signal's responsiveness.

**Verdict for B: CONDITIONAL APPROVE.** Must address:
1. Explain specifically why HMM regime detection differs from R17's regime-adaptive approach in a way that changes the cost-adjusted outcome
2. Pre-commit to minimum OOS requirements: >= 20 days OOS, >= 100 trades, Sharpe SE < 0.15
3. Limit to 2-state HMM with minimal side information to control parameter count
4. Use corrected cost: 3.92 pts (1.19 bps), not 4.0 pts (1.33 bps)

---

## Candidate C: Volatility-Regime Breakout (VRB)

### Challenge C-1: "Volatility Compression Precedes Breakouts" is a Well-Known Pattern -- What Evidence Exists for TMFD6 Specifically?

**Claim challenged**: Survey claims "Quiet regimes precede breakouts" and expects IC 0.05-0.12 at 1-4h horizon.

**Objection**: Volatility compression -> breakout is a generic pattern observed across many markets. The survey cites papers on S&P 500 volatility forecasting (Blake et al.) and general timescale separation (Rosenzweig), but provides ZERO evidence that this pattern exists on TMFD6 specifically.

From R17's regime analysis on TMFD6:
- Spread-volatility correlation is low (+0.051 per Stage 2a adverse selection analysis)
- The most volatile periods are opening (08:45-09:15) and night session -- these are TIME-OF-DAY effects, not compression -> expansion dynamics
- R17's C2 time-of-day analysis showed no single bucket has both |mean bps| > 4 AND consistency > 60%

The critical question: does TMFD6 actually exhibit volatility compression -> expansion cycles at intraday frequency, or is its volatility pattern predominantly driven by time-of-day seasonality? If the latter, VRB is just a fancy version of the R17 C2 time-of-day pattern (which FAILED the kill gate).

**Data required**: Before prototyping, measure:
1. Count of vol-compression-to-expansion transitions per session (RV_5min/RV_1h > 2.0 after RV_1h < P20)
2. Directional bias after these transitions (does the 4h EMA slope actually predict breakout direction?)
3. Compare VRB trigger distribution to pure time-of-day triggers -- if >80% of VRB triggers occur at the same times every day, VRB is just time-of-day in disguise

**Severity**: HIGH. The hypothesis is plausible but completely untested on TMFD6.

### Challenge C-2: 1-3 Trades/Session Creates a Dangerous Low-N Problem

**Claim challenged**: Survey estimates 1-3 trades per session, yielding 58-174 trades over the 58-day dataset.

**Objection**: At 1-3 trades/session:
- **Total N over 58 days**: ~60-180 trades
- **Train/test split**: With rolling 20-day calibration, OOS N ~ 40-120 trades
- **Sharpe estimation**: SE(Sharpe) ~ 1/sqrt(N). At N=80, SE = 0.11. Cannot distinguish Sharpe 0.3 from Sharpe 0.0.
- **p-value**: For a mean return test, need sqrt(N) * mean/std > 1.96. At N=80 and typical intraday Sharpe ~0.5/sqrt(252) per day, the test has very low power.

This is the same problem that plagued R17's C1 Gap Fade (N=27) and C4 Thursday Night Short (N=7). Both showed strong returns but could not achieve statistical significance.

Furthermore, the survey claims VRB is "complementary to CBS." But CBS also trades infrequently (2-5 trades/day when conditions trigger). Running two low-frequency strategies does not solve the statistical validation problem -- it doubles the number of hypotheses while keeping individual N low.

**Severity**: MEDIUM-HIGH. Not a flaw in the signal itself, but a practical limitation for validation with available data. Must pre-commit to a minimum-N kill gate.

### Challenge C-3: The "Breakout Direction" Component (4h EMA Slope) is Unvalidated

**Claim challenged**: Survey proposes using "the sign of the slow-moving trend (e.g., 4h EMA slope) to predict breakout direction."

**Objection**: R17 measured trend indicators on TMFD6:
- phi_64min (closest to 4h EMA): IC = -0.035, t = -0.9 (NOT significant)
- phi_32min: IC = +0.012, t = 0.5 at 30min forward (NOT significant)

The 4h EMA slope has no demonstrated predictive power on TMFD6 at any forward horizon. Using it as the directional component of VRB means half the strategy (vol compression detection) may work, but the other half (direction prediction) is likely noise.

If direction prediction fails, the strategy degenerates into: "enter randomly after vol compression, hope the breakout goes your way." At 50% directional accuracy minus costs, expected P&L is negative.

**Alternative**: Instead of 4h EMA slope, consider using the direction of the vol expansion itself (sign of the first 5min return after RV ratio triggers). This is reactive rather than predictive but avoids relying on the unvalidated 4h trend signal.

**Severity**: HIGH. The directional component is the weakest link, and without it VRB has no directional edge.

**Verdict for C: CONDITIONAL APPROVE.** Must address:
1. Measure actual vol-compression -> expansion transition count on TMFD6 (is it time-of-day or genuine compression?)
2. Test breakout direction prediction independently before building the full strategy
3. Pre-commit to minimum-N: >= 80 OOS trades for Stage 2 to count as valid
4. Consider reactive direction (enter in direction of the emerging move) instead of predictive (4h EMA)
5. Use corrected cost: 3.92 pts (1.19 bps)

---

## Cross-Cutting Challenges

### Challenge X-1: The Survey Uses Stale Cost Numbers Throughout

Every IC breakeven calculation in the survey uses 1.33 bps (4.0 pts). The corrected cost is 1.19 bps (3.92 pts). This makes all breakeven thresholds ~11% lower:
- 30min breakeven: 0.037 (not 0.043)
- 60min breakeven: 0.026 (not 0.030)
- 4h breakeven: 0.013 (not ~0.015)

This slightly improves feasibility for all candidates but does not change any verdict.

### Challenge X-2: All Three Candidates Share the Same Structural Weakness -- 58 Days

The survey acknowledges "58 days is TIGHT" for A and "adequate" for B and C. In reality, 58 days is challenging for ALL three:
- **A**: 20-day rolling calibration leaves 38 OOS days -- too few for cubic model with 3 parameters
- **B**: 30-day HMM calibration leaves 28 OOS days -- borderline for 7-parameter model
- **C**: 20-day RV percentile calibration leaves 38 OOS days -- adequate for the signal, but 1-3 trades/day yields low N

The honest assessment: none of these strategies can be VALIDATED with 58 days. They can be EXPLORED (measure IC, estimate fill rates, check for gross errors), but no strategy can be promoted from 58 days of data. This should be stated explicitly as a scope limitation.

### Challenge X-3: FeatureEngine v2 Has 21 Features, Not 18 -- Are Any Directly Usable?

The survey does not mention FeatureEngine v2 features, which include:
- `ret_autocov_5s_x1e6` [17] -- directly relevant to regime detection
- `tob_survival_ms` [18] -- relevant to fill probability
- `ofi_depth_norm_ppm` [16] -- normalized OFI
- phi_8min is already in the pipeline as a momentum feature

Candidates B and C could benefit from incorporating existing features rather than building from scratch. The survey should explicitly state which FeatureEngine v2 features are relevant and whether they would be used.

---

## Summary Table

| # | Challenge | Target | Severity | Resolution |
|---|-----------|--------|----------|------------|
| A-1 | Cubic reversion already tested R17 -- c term NOT significant on TMFD6 | A (TSM-CR) | **FATAL** | REJECT: re-proposal of killed signal |
| A-2 | Multi-asset R^2 does not translate to single-instrument IC | A (TSM-CR) | HIGH | Contradicted by R17 measurements |
| B-1 | R17 regime conditioning failed to overcome cost barrier | B (HMM-RCM) | HIGH | Must explain why HMM differs from R17 approach |
| B-2 | 58 days insufficient for HMM calibration + OOS validation | B (HMM-RCM) | HIGH | Pre-commit min-N and min-OOS requirements |
| B-3 | "No time-lag" claim overstated for filtered HMM | B (HMM-RCM) | MEDIUM | Correct the claim |
| C-1 | No evidence vol compression -> breakout exists on TMFD6 | C (VRB) | HIGH | Measure transitions before prototyping |
| C-2 | 1-3 trades/session = low-N validation problem | C (VRB) | MEDIUM-HIGH | Pre-commit min-N >= 80 OOS |
| C-3 | 4h EMA slope has no predictive power on TMFD6 (R17 data) | C (VRB) | HIGH | Test direction prediction independently |
| X-1 | Stale cost: 1.33 bps should be 1.19 bps | All | LOW | Correct throughout |
| X-2 | 58 days insufficient for validation of any candidate | All | HIGH | Acknowledge as scope limitation |
| X-3 | FeatureEngine v2 (21 features) not referenced | B, C | MEDIUM | Map relevant features |

---

## Recommended Stage 2 Approach

**Do NOT proceed with Candidate A.** It is a re-proposal of R17 MSTR with the same paper, same model, same instrument. The cubic reversion coefficient is empirically absent on TMFD6. The momentum component (phi_8min) already exists.

**For Candidate B (HMM-RCM)**:
1. Start with a data diagnostic: fit 2-state HMM on TMFD6 5min bars, report state means/variances/transition matrix. If state means are not distinguishable (|mu_1 - mu_2| < 2*sigma), the HMM cannot separate regimes and the candidate is dead.
2. Compare HMM regime labels to R17's spread+rvol regime labels. If correlation > 0.7, HMM is redundant with already-tested approach.
3. Only proceed to strategy simulation if step 1 shows distinguishable states AND step 2 shows HMM provides new information.

**For Candidate C (VRB)**:
1. Measure vol-compression -> expansion event count on TMFD6 (how many triggers per session?). If < 1/session, N is too low.
2. Test directional prediction independently: after each trigger, does 4h EMA slope predict next-1h return sign? Compare to random. If directional accuracy < 55%, replace with reactive direction.
3. Only build full strategy if steps 1 and 2 pass.

**Priority**: C > B (lower complexity, lower overfitting risk, more complementary to CBS).

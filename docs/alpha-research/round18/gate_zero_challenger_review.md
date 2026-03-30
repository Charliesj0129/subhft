# Gate-Zero Challenger Review: OFI Persistence Results

**Date**: 2026-03-26
**Reviewer**: Challenger Agent
**Artifact**: `docs/alpha-research/round18/gate_zero_ofi_persistence.md`
**Status**: Reviewing kill-gate decision for Candidates A (log-GOFI) and B (OFI-OU)

---

## VERDICT: NARROW PASS with significant caveats. TMFD6 log-OFI r=0.102 at 5min is real but economically marginal.

The result survives the kill gate (pred corr >= 0.05 at 5min) but the challenges below substantially temper the optimism in the Researcher's interpretation. The signal exists. The question is whether it is economically exploitable.

---

## Challenge 1: The r=0.102 at 5min Explains Only 1% of Variance -- Economically Marginal

**Claim challenged**: The report frames r=0.102 as "STRONGEST signal at 5min" and concludes Candidates A and B SURVIVE. The narrative emphasis on statistical significance (p=0.005) creates an impression of a meaningful trading signal.

**Objection**: r=0.102 means R-squared = 0.0104. The signal explains **1.04% of the variance** in next-5-minute returns. The remaining 98.96% is noise. For context:

1. **IC-to-PnL translation**: An IC (rank correlation, similar magnitude to Pearson r for near-normal distributions) of 0.10 on a 5-minute signal with ~60 non-overlapping observations per session yields very modest Sharpe. Using the fundamental law of active management: IR = IC * sqrt(breadth). With IC=0.10 and breadth=60: IR = 0.10 * 7.75 = 0.775. This is a pre-cost annualized IR < 1.0 -- marginal for any strategy, and this is BEFORE the 3.92 pts RT cost on TMFD6.

2. **R17 cost model check**: The kill gate was set at IC > 0.043 at 30min, derived from the cost model. But at 5min holding, with 60 trades/day at 3.92 pts RT cost = 235 pts daily cost. To break even, need 235 pts gross alpha. At TMFD6's typical daily range of ~50-150 pts, extracting 235 pts gross from a signal that explains 1% of variance is extremely unlikely.

3. **The kill gate was set too low**: The original Challenger review (Challenge 1) requested predictive IC > 0.03 at 5min as the kill gate. The Researcher used >= 0.05. Both thresholds are too lenient for a signal that must overcome 3.92 pts RT cost. A realistic kill gate should be IC > 0.10 at the intended holding period, which this barely meets and only for log-OFI on TMFD6.

**Impact**: The result passes the stated kill gate but should not be interpreted as evidence of a tradable signal. It is evidence that OFI has measurable (but tiny) predictive power at 5 minutes on TMFD6. Whether this translates to positive net PnL remains unproven.

**Severity**: HIGH. Statistical significance != economic significance. p=0.005 with N=739 just means the effect is non-zero, not that it is large enough to trade.

---

## Challenge 2: Cross-Session Contamination in Predictive Correlations

**Claim challenged**: The methodology section states "Aggregate OFI into time buckets, compute contemporaneous and predictive (lag-1) correlations" with "Trading hours filter: 08:45-13:45."

**Objection**: The raw data reveals a significant discrepancy between `n_buckets` and `n_pred`:

| Instrument | Horizon | n_buckets | n_pred | Dropped |
|---|---|---|---|---|
| TMFD6 | 30s | 6458 | 5671 | 787 (12.2%) |
| TMFD6 | 1m | 3537 | 3249 | 288 (8.1%) |
| TMFD6 | 5m | 773 | 739 | 34 (4.4%) |
| TXFD6 | 30s | 2502 | 1929 | 573 (22.9%) |
| TXFD6 | 5m | 443 | 367 | 76 (17.2%) |

The dropped pairs are presumably cross-session boundaries (last bucket of day N cannot predict first bucket of day N+1). This is correct methodology. However:

1. **Were overnight gaps handled?** If the last 5min bucket before 13:45 close is paired with the first 5min bucket after 08:45 open the next day, this introduces ~19 hours of overnight information into a "5-minute prediction." The report does not explicitly state that cross-day pairs were excluded. The drops (34 out of 773 at 5min for TMFD6, ~0.6 per day over 58 days) are consistent with dropping only session-boundary pairs, which is correct. But the report should confirm this explicitly.

2. **Opening auction contamination**: The 08:45 opening on TMFD6 features an auction mechanism. The first few minutes have different microstructure (price discovery, accumulated overnight orders). If opening buckets are included, they may inflate OFI-return correlation because the opening auction creates large OFI with correspondingly large returns. Are the first N minutes excluded? The report does not say.

3. **Were the predictive pairs strictly non-overlapping?** For 5min buckets, bucket t = [0:00-5:00] predicts bucket t+1 = [5:00-10:00]. If buckets overlap even partially (e.g., rolling windows rather than discrete buckets), the predictive correlation is contaminated by contemporaneous information.

**Data required to resolve**:
1. Confirm cross-day pairs were excluded (the numbers suggest yes, but state explicitly).
2. Report results with the first 15 minutes (08:45-09:00) excluded. If the correlation drops materially, the signal is an opening-auction artifact.
3. Confirm discrete (non-overlapping) bucketing, not rolling windows.

**Severity**: MEDIUM. The numbers are consistent with correct handling, but the report should be explicit. The opening-auction question is the most important sub-check.

---

## Challenge 3: TMFD6 Contemporaneous r=0.748 Inflates Expectations Dangerously

**Claim challenged**: The report highlights "Extraordinary contemporaneous correlation (logOFI: 0.73-0.76 from 30s to 30min)" as Key Finding #2.

**Objection**: The contemporaneous correlation is irrelevant for a trading strategy and its prominence in the report creates a dangerous anchoring effect. Readers (and future Stage 2 researchers) will subconsciously anchor on r=0.748 rather than the actual predictive r=0.102.

Specific concerns:

1. **TMFD6's thin book inflates contemporaneous correlation mechanically**: With only 4.1 lots average depth at L1 and 1.8 ticks/sec, a single 1-lot aggressive order can move the mid-price by 1 point AND create a large OFI signal simultaneously. On a thicker book (like TXFD6 with more depth), the same order creates OFI but does NOT move the price -- hence lower contemporaneous correlation. The r=0.748 on TMFD6 vs r=0.493 on TXFD6 is NOT evidence that TMFD6 is "better for OFI signals" -- it is evidence that TMFD6's thin book creates a tighter mechanical coupling between OFI and price. This mechanical coupling does not help prediction.

2. **The gap between contemporaneous (0.748) and predictive (0.102) is 0.646**: This 86% drop from contemporaneous to predictive is the empirical measure of how much of the OFI-price relationship is contemporaneous (non-tradable) vs persistent (potentially tradable). The Researcher frames this as "log-OFI outperforms standard OFI at 5min" -- true, but the absolute level of the predictive signal is still tiny.

3. **Recommendation**: The contemporaneous correlation should be reported as a sanity check (confirms data pipeline correctness) but REMOVED from the narrative about signal strength. Any mention of r > 0.7 should be accompanied by an immediate caveat that this is contemporaneous and not tradable.

**Severity**: MEDIUM-HIGH. This is about preventing cognitive bias in downstream analysis. The 0.748 number will anchor expectations and make the 0.102 feel "disappointingly low" rather than being evaluated on its own economic merits.

---

## Challenge 4: TXFD6 "Borderline Fail" is Actually a Clear Fail

**Claim challenged**: TXFD6 is classified as "BORDERLINE FAIL" with the recommendation to defer "pending more data or full multi-level GOFI implementation."

**Objection**: TXFD6 results are unambiguously negative:

1. **No horizon achieves statistical significance**: The best TXFD6 predictive result is log-OFI at 5min: r=0.062, p=0.236. This is p > 0.20 -- not even close to marginal significance. Calling this "borderline" misrepresents the data.

2. **Negative predictive correlation at 10min+ (r=-0.092, r=-0.080)**: This means OFI predicts REVERSAL at longer horizons on TXFD6. This is the opposite of what Candidates A and B need (momentum/continuation).

3. **The "more data" excuse is weak**: At N=367 pairs (5min), achieving significance at r=0.062 would require approximately N > 1,000. That is ~160 trading days of additional data (~8 months). This is not a reasonable deferral timeline.

4. **The "full multi-level GOFI" excuse shifts goalposts**: The test was designed as a kill gate for the OFI persistence hypothesis. If L1 OFI shows no predictive power, arguing that L2-L5 will save it is speculation. Multi-level OFI adds information about the current state of the book, but it does not fundamentally change the decay dynamics of flow-price impact.

**Verdict**: TXFD6 should be classified as **FAIL**, not "borderline fail." Candidates A and B should proceed on TMFD6 only. If anyone wants to revisit TXFD6, the burden of proof is on them to show that multi-level construction creates persistence where L1 has none.

**Severity**: MEDIUM. The practical impact is small since the report already focuses on TMFD6, but the framing matters for intellectual honesty.

---

## Challenge 5: Autocorrelation AC(1) = 0.116 at 5min Does Not Support "Persistent Memory"

**Claim challenged**: Key Finding #4 states "AC(1) ranges 0.10-0.26 across horizons, confirming OFI has memory (consistent with Hu & Zhang 2025). This memory is what makes the OU model appropriate."

**Objection**: AC(1) = 0.116 at 5min on TMFD6 means 11.6% of the variance in the current 5-minute OFI is predictable from the previous 5-minute OFI. This is very weak autocorrelation.

1. **OU model appropriateness**: The OU model assumes a mean-reverting process with exponential autocorrelation decay: AC(lag) = exp(-kappa * lag). With AC(1) = 0.116 at 5min lag, kappa = -ln(0.116)/5 = 0.43/min. The half-life of OFI memory is ln(2)/0.43 = 1.6 minutes. This means OFI on TMFD6 has a half-life of ~1.6 minutes -- it is 75% decayed by 5 minutes and 94% decayed by 10 minutes. The OU model is "appropriate" in the sense that OU fits any mean-reverting process, but the estimated kappa gives a SHORT memory, not the persistent memory the report claims.

2. **Comparison with Hu & Zhang (2025)**: The paper found OFI correlation > 0.50 at 60min on CSI 300. Our TMFD6 AC(1) = 0.116 at 5min implies AC at 60min would be approximately 0.116^12 = essentially zero. This directly contradicts the claim of consistency with Hu & Zhang. The TMFD6 OFI memory structure is fundamentally different from CSI 300.

3. **Implication for Candidate B (OFI-OU Regime)**: The OU model will work, but it will correctly identify that TMFD6 OFI has fast mean-reversion (kappa ~ 0.43/min). The "optimal horizon" from the quasi-Sharpe framework will likely be 1-3 minutes -- at which point we are back to the short-horizon regime where costs dominate. The OU framework does not CREATE persistence; it measures what exists.

**Severity**: HIGH. The report frames AC(1)=0.10-0.26 as supporting the OU hypothesis. In reality, these values show that OFI memory on our instruments is much weaker than on CSI 300, which undermines the core premise of Candidate B.

---

## Challenge 6: No Look-Ahead Bias Detected, but Multiple-Testing Risk Not Addressed

**Claim challenged**: The methodology appears clean (lag-1 prediction, winsorization, trading hours filter).

**Assessment**: I do NOT detect look-ahead bias in the methodology. The predictive correlations use strictly lagged buckets (OFI in bucket t predicts return in bucket t+1). The winsorization at 1st/99th percentiles is standard. However:

1. **Multiple testing**: The report tests 2 instruments x 6 horizons x 2 OFI variants = 24 tests. The headline result (log-OFI TMFD6 5min, p=0.005) survives Bonferroni correction (0.05/24 = 0.002) ONLY marginally -- p=0.005 > 0.002. With Holm-Bonferroni (less conservative), it survives. The report does not mention multiple testing at all.

2. **The "best" result was selected post-hoc**: The report highlights "STRONGEST signal at 5min (r=0.102, p=0.005)" -- but this was the best result across 12 TMFD6tests. Reporting the best result from a grid without adjustment is a form of selection bias.

3. **Practical recommendation**: Apply Benjamini-Hochberg FDR correction to the 12 TMFD6 p-values. Report the adjusted p-value for the 5min log-OFI result. If it remains < 0.05 after correction, the finding is robust. My estimate: at 12 tests and p=0.005, the BH-adjusted p-value is approximately 0.005 * 12/1 = 0.06 -- marginal. Several other TMFD6 results (30s, 1m, 2m) also have p < 0.01, which helps with FDR.

**Severity**: LOW-MEDIUM. The result likely survives FDR correction given that multiple horizons show significance on TMFD6. But the report should acknowledge the issue.

---

## Summary Table

| # | Challenge | Severity | Verdict Impact |
|---|-----------|----------|---------------|
| 1 | r=0.102 explains 1% of variance -- economically marginal | HIGH | Signal exists but PnL viability unproven |
| 2 | Cross-session handling not explicitly confirmed | MEDIUM | Likely correct, needs explicit statement |
| 3 | Contemporaneous r=0.748 anchoring bias | MEDIUM-HIGH | Remove from signal narrative |
| 4 | TXFD6 is a clear FAIL, not "borderline" | MEDIUM | Reclassify to FAIL |
| 5 | AC(1)=0.116 implies 1.6min half-life, not "persistent memory" | HIGH | Undermines Candidate B's OU premise |
| 6 | Multiple testing not addressed | LOW-MEDIUM | Likely survives FDR but should report |

---

## Revised Kill Gate Assessment

| Test | Original Verdict | Challenger Verdict | Reasoning |
|---|---|---|---|
| TMFD6 log-OFI 5min pred | PASS (r=0.102) | **NARROW PASS** | Statistically real, economically unproven |
| TXFD6 log-OFI 5min pred | BORDERLINE FAIL | **FAIL** | p=0.236, not even close to significant |
| OFI persistence supports OU model | Claimed yes | **WEAK** | AC half-life ~1.6min, much shorter than CSI 300 |

---

## Conditions for Stage 2 Continuation

1. **MANDATORY**: Stage 2 must include a **simple backtest** (not just IC measurement). Enter long/short when 5min log-OFI exceeds +/- 1 sigma threshold, hold 5 minutes, compute net PnL after 3.92 pts RT cost + 2 pts estimated slippage per side. If net PnL per trade is negative, kill both candidates.

2. **MANDATORY**: Report results with opening 15 minutes (08:45-09:00) excluded. If the 5min predictive r drops below 0.05 without the opening, the signal is an opening-auction artifact.

3. **MANDATORY**: Reclassify TXFD6 as FAIL. Do not carry it forward as "deferred."

4. **MANDATORY**: Recompute expected OU half-life from autocorrelation data. If half-life < 2 minutes, acknowledge that the "optimal horizon" from quasi-Sharpe will likely be in the 1-3 minute range where costs dominate, which undermines the medium-frequency thesis.

5. **RECOMMENDED**: Apply Benjamini-Hochberg FDR correction across all 12 TMFD6 tests. Report adjusted p-values.

6. **RECOMMENDED**: Remove contemporaneous correlation from the signal strength narrative. Report it as a data quality check only.

---

*Challenger Agent -- R18 Gate-Zero Review*

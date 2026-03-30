# Round 20 — Stage 1 Challenger Review

**Date**: 2026-03-27
**Reviewer**: Claude (Challenger agent)
**Survey under review**: `round20_stage1_l2_lob_survey.md`

---

## Candidate A: Cross-Asset L5 OFI with PCA Integration (2330 -> TXFD6)

### Challenge 1: Temporal overlap is catastrophically thin — PCA calibration will overfit

The survey claims "directly feasible" with overlapping L5 data. I verified the actual data:

| Date | TXFD6 L5 rows | 2330 L5 rows | Both present? |
|------|--------------|-------------|---------------|
| 2026-02-06 | 412,892 | 86,435 | YES |
| 2026-02-11 | 4,096 | 131 | UNUSABLE (131 rows) |
| 2026-02-23 | 433,487 | 125,310 | YES |
| 2026-02-24 | 397,715 | 42,622 | YES |
| 2026-02-25 | 138,732 | 73,998 | YES |
| 2026-03-02 | 0 | 84,188 | NO (no TXFD6) |
| 2026-03-04 | 0 | 81,901 | NO (no TXFD6) |
| 2026-03-05 | 0 | 3,895 | NO (no TXFD6) |
| 2026-03-17 | 0 | 76 | NO |
| 2026-03-20 | 29,294 | 0 | NO (no 2330) |
| 2026-03-23 | 75,604 | 38,914 | YES |

**Usable overlapping days: 5 (Feb-06, Feb-23, Feb-24, Feb-25, Mar-23).** Feb-11 has 131 rows of 2330 — unusable. Mar-20 and Mar-23 TXFD6 have far fewer rows than the Feb dates (75K vs 400K), suggesting partial capture.

PCA on 5-level OFI requires fitting principal components on cross-asset aligned data. 5 overlapping days is **grossly insufficient** for:
- In-sample PCA calibration (need stable eigenvectors)
- Out-of-sample validation (zero OOS days remain after calibration)
- Any claim of statistical significance

The survey's IC estimate of 0.07-0.10 is extrapolated from R17's L1-only IC=0.061 with a speculative "L5 should help" markup. R18 proved definitively that **L2-L5 adds nothing over L1** on both 2330 (L1 IC=0.217 vs full MLOFI IC=0.206) and TXFD6 (IC near zero regardless). Adding PCA does not change the underlying information content.

### Challenge 2: R17 expanded validation already killed the 2330->TXFD6 lead-lag at L1

The survey cites R17 IC=0.061 as the baseline to improve upon. But R17's **22-day expanded validation** (non-overlapping windows) showed:

- **Best IC: +0.061, p=0.066** — fails p<0.05 significance
- **Bootstrap CI: [-0.045, +0.088]** — includes zero
- **Sign consistency: 57%** — not significantly above coin flip
- **Net edge: +1.01 bps** after 1.33 bps TMFD6 cost — fails the >3 bps gate
- **Per-day IC range: -0.278 to +0.289** — extreme instability

The claim that "adding L5 OFI integration could push over the threshold" contradicts R18's finding that L1 alone is *better* than multi-level on both assets. PCA is a linear transformation — it cannot create information that isn't there. If the L1 lead-lag signal has p=0.066 over 22 days, adding 4 noisy levels through PCA will *degrade* it, not improve it.

### Challenge 3: "Natural detrending" claim is unsubstantiated

The survey claims cross-asset OFI is "inherently detrended" because it uses a different asset's order flow. This is wrong. If 2330 and TXFD6 co-trend (they do — TSMC is ~30% of TAIEX), then 2330-OFI during an uptrend will be positive, and TXFD6 forward returns will also be positive. The cross-asset nature does NOT remove trend contamination — it may even amplify it because both assets respond to the same macro factor.

R18's detrended IC gate is mandatory. The survey provides no detrended IC estimate for the cross-asset signal — only a hand-wave that it should be safe.

### Challenge 4: TXFD6 L5 book is too thin for meaningful multi-level OFI

I verified TXFD6 L5 volume statistics:

| Level | Median vol | P90 vol | P99 vol |
|-------|-----------|---------|---------|
| L1 | 1 | 3 | 6 |
| L2 | 1 | 3 | 10 |
| L3 | 1 | 4 | 8 |
| L4 | 1 | 3 | 26 |
| L5 | 1 | 3 | 10 |

**Median volume is 1 contract at every level.** The OFI delta between consecutive snapshots is dominated by single-lot changes. PCA on 5 levels of mostly-binary signals (0 or 1 lot change) will produce eigenvalues dominated by noise. This is why R15 found "L3-L5 add noise" — not because the methodology was wrong, but because the data is structurally insufficient for multi-level analysis on TXFD6.

**Verdict: REJECT**

Fundamental flaws:
1. Only 5 overlapping days — impossible to calibrate PCA + validate OOS
2. R17 expanded validation already killed the underlying lead-lag (p=0.066, CI includes zero)
3. R18 proved L2-L5 adds nothing on both assets
4. TXFD6 L5 volumes too thin for meaningful multi-level OFI (median 1 lot)
5. "Natural detrending" claim is false — co-trending assets don't detrend each other

---

## Candidate B: LOB Shape Regime Detection via Snapshot Clustering

### Challenge 1: Clustering on thin, near-uniform books is degenerate

TXFD6 has median volume = 1 at all 5 levels. The "shape" of the LOB is overwhelmingly [1,1,1,1,1] on both sides, with occasional spikes. K-means clustering on vectors where 90%+ of values are in {0,1,2,3} will produce:
- One dominant cluster (the [~1,~1,~1,~1,~1] book)
- One or two "outlier" clusters capturing rare large resting orders
- Extreme sensitivity to the number of clusters and feature scaling

The ClusterLOB paper (Zhang et al. 2025) was validated on NASDAQ stocks with meaningful depth at each level. TXFD6's thin book makes the clustering degenerate — most snapshots will fall in the same cluster, providing no regime discrimination.

**10 days of L5 data with degenerate clusters = pure overfitting.**

### Challenge 2: Regime gating amplifies the multiple-testing problem

The survey proposes regime-conditional OFI: "the same OFI value has different predictive content depending on the current LOB shape regime." This is equivalent to testing OFI-IC in K x T subsets (K clusters x T time slices), then selecting the best one. With K=3-5 clusters, 10 days, and multiple horizons, the effective number of hypothesis tests is 30-50+.

Without a rigorous multiple-testing correction (which the survey doesn't mention), any "significant" finding is likely false discovery. The comparison to CBS's time-of-day gating is misleading — CBS gating was validated on 40+ days with a clear economic rationale (opening auction dynamics). LOB shape regimes have no such prior.

### Challenge 3: R15 collinearity finding applies directly

R15 found LOB momentum (which uses the same L5 depth data) has Spearman r=+0.703 with depth_imbalance. Any regime derived from L5 shape features will be heavily correlated with depth_imbalance, which is already in the FeatureEngine. The "regime filter" framing doesn't escape this — if the regime is defined by the same features that are already used, conditional OFI is just OFI x depth_state, which was implicitly tested in prior rounds.

**Verdict: REJECT**

Fundamental flaws:
1. TXFD6 book too thin — clustering on near-uniform [1,1,1,1,1] vectors is degenerate
2. 10 days + multiple cluster configs = extreme overfitting with no OOS
3. Multiple-testing problem: regime-conditional IC across K clusters x horizons
4. Collinear with existing depth_imbalance feature (R15: r=0.703)

---

## Candidate C: Trade Co-occurrence Conditional OFI

### Challenge 1: TXFD6 tick interval vs co-occurrence threshold ambiguity

TXFD6 median tick interval is 125ms. The co-occurrence classification requires a threshold to distinguish "isolated" (no nearby trades) from "clustered" (burst of trades). With a median interval of 125ms:

- Threshold < 125ms: ~50% of trades are "isolated" and ~50% "clustered" — binary split at the median, highly sensitive to exact threshold
- Threshold > 125ms: most trades are "isolated" — the clustered category becomes too small for statistical power
- Threshold << 125ms: most trades are "clustered" — the isolated category collapses

The Lu et al. (2022) paper used **daily** equity data with trade counts as the grouping variable, not intraday millisecond-level co-occurrence windows. The translation from daily buckets to 125ms-interval futures ticks is a major methodological leap that the survey acknowledges as a risk but underestimates.

**Required data response**: Before proceeding, compute the empirical distribution of inter-trade intervals on TXFD6 and show that the isolated/clustered split is robust to +/-50% threshold perturbation. If the IC flips sign or drops below 0.01 with 50% threshold change, the signal is an artifact of the threshold.

### Challenge 2: No trade-side classification on TAIFEX = degraded signal

Lu et al.'s COI requires classifying trades as buyer-initiated or seller-initiated. TAIFEX does not provide trade side. The survey notes "Must infer from price movement" — this means Lee-Ready or tick rule inference, which has known error rates of 15-30% on actively traded futures.

This is not a minor issue. The entire COI framework relies on decomposing order imbalance by trade type. A 15-30% classification error rate dilutes the signal proportionally. If the true isolated-trade IC is 0.04, classification noise could push observed IC to 0.025-0.03 — dangerously close to the 0.030 cost breakeven at 60s.

### Challenge 3: Lu et al. results are daily equity — not intraday futures

The paper reports "conspicuous returns and Sharpe ratios" on **457 stocks over 4 years at daily frequency**. The survey's estimated IC of 0.02-0.05 at 30-120s is not derived from any intraday evidence. It's a hopeful extrapolation from a fundamentally different setting:

- Daily equity: large N (457 stocks x 1000 days), long horizon, lower costs
- Intraday TXFD6: single instrument, 40 days, 30-120s horizon, 2.0 bps RT cost

R19 established that "HF microstructure signals CANNOT extend to MF via math transforms." The inverse is equally suspect: MF/daily effects cannot be assumed to hold at HF/intraday. The survey provides no intraday evidence for the co-occurrence effect.

**Verdict: CONDITIONAL APPROVE**

Despite the challenges, Candidate C has redeeming qualities:
1. Uses abundant L1 data (40+ days) — best data position of all candidates
2. Lowest implementation complexity
3. Novel decomposition not tested in R15-R19
4. Proposed as CBS filter, not standalone — lower bar to clear

**Conditions for Stage 2 approval:**
1. **Threshold robustness test (BLOCKING)**: Compute inter-trade interval distribution on TXFD6. Show that isolated/clustered classification is robust to +/-50% threshold perturbation by measuring IC stability across 5+ thresholds. IC sign reversal under perturbation = KILL.
2. **Detrended IC from day 1**: All IC computations must use 5-minute detrended forward returns. No raw IC allowed.
3. **Use L1 trade data only**: Do NOT involve L5 data. Keep it simple — the value proposition is trade timing decomposition, not depth features.
4. **Non-overlapping windows mandatory**: No overlapping IC inflation. Report per-day IC with bootstrap CI.
5. **Trade classification error analysis**: Report what fraction of trades are ambiguous under tick rule. If ambiguous rate > 25%, consider alternative decompositions (e.g., volume-weighted rather than direction-weighted).

---

## Final Verdict: CONDITIONAL APPROVE (C only)

| Candidate | Verdict | Rationale |
|-----------|---------|-----------|
| A: Cross-Asset L5 OFI | **REJECT** | 5 overlapping days, R17/R18 already killed the components, thin book, false detrending claim |
| B: LOB Shape Regime | **REJECT** | Degenerate clustering on thin book, 10 days, multiple-testing, collinear with existing features |
| C: Trade Co-occurrence COI | **CONDITIONAL APPROVE** | Novel, good data, but threshold robustness and detrended IC must be demonstrated first |

### Unresolved Challenges

**Candidate A (3 unresolved)**:
1. 5 overlapping days insufficient for PCA — no resolution possible without more data
2. R17 expanded validation killed the lead-lag at p=0.066 — adding noise (L2-L5) won't fix this
3. Natural detrending claim is false for co-trending assets

**Candidate B (3 unresolved)**:
1. Degenerate clustering on thin book — no resolution possible with current data
2. 10 days + clustering parameters = overfitting — no resolution possible without more data
3. Collinear with existing depth_imbalance

**Candidate C (2 unresolved, but resolvable)**:
1. Threshold robustness — resolvable via empirical test in Stage 2
2. Trade classification error rate — resolvable via sensitivity analysis in Stage 2

### Recommendation to Researcher

If Candidate C's threshold robustness test fails in Stage 2, the entire L2-LOB direction should be paused until:
1. More L5 data accumulates (20+ overlapping days for cross-asset work)
2. True MBO data becomes available (for ClusterLOB-style approaches)
3. TXFE6 or TX (not mini) L5 data is tested (thicker books)

The structural problem across A and B is data insufficiency combined with TXFD6's thin book. No methodology can overcome median 1-lot depth at all 5 levels.

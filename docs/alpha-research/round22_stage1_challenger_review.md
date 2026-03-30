# Round 22 -- Stage 1 Challenger Review: LOB Slope & Convexity

**Date**: 2026-03-28
**Reviewer**: Challenger Agent
**Survey under review**: `round22_stage1_literature_survey.md`

---

## 1. Overall Assessment: REJECT

This survey proposes three LOB depth-shape candidates (DWSA, RC-OFI, DCI) that collectively represent the **fourth attempt** to extract alpha from L2-L5 depth data on TAIFEX futures. The three prior attempts (R11, R15, R20) all failed, and the survey does not present sufficient evidence that R22 will produce a materially different outcome. The fundamental constraint -- TXFD6/TMFD6 L5 volumes are too thin for multi-level analysis -- remains unaddressed by reframing the same depth data through different mathematical transformations.

The survey is well-written and the literature review is thorough. However, good citations do not override empirical evidence from our own market. The survey itself acknowledges (Section 5) that Gate Zero may kill all three candidates, which raises the question: **why was this survey written before running Gate Zero?**

---

## 2. Specific Challenges

### Challenge 1: TXFD6 L5 depth is structurally insufficient -- median volume is 1 lot at ALL levels

**Evidence**: The R20 Challenger Review (round20_stage1_challenger_review.md) verified TXFD6 L5 volume statistics directly from ClickHouse data:

| Level | Median vol | P90 vol | P99 vol |
|-------|-----------|---------|---------|
| L1 | 1 | 3 | 6 |
| L2 | 1 | 3 | 10 |
| L3 | 1 | 4 | 8 |
| L4 | 1 | 3 | 26 |
| L5 | 1 | 3 | 10 |

**Median volume is 1 contract at every level.** This is devastating for all three R22 candidates:

- **DWSA**: `bid_concentration = vol_bid[1] / sum(vol_bid[1:3])` becomes `1 / (1+1+1) = 0.33` at the median. The signal will be dominated by the discrete jumps between 0 and 1 lot, producing a ternary signal (all-at-L1, evenly-split, or zero) rather than a continuous measure of depth shape. The "slope asymmetry" is noise when the underlying data is quantized to {0, 1, 2} lots.

- **DCI**: `vol[3] - vol[2]` at the median is `1 - 1 = 0`. At P90 it is `4 - 3 = 1`. The "convexity" signal is literally a coin flip between -1, 0, and +1. Calling this a "second derivative of the depth profile" is mathematically valid but practically meaningless -- you cannot extract a smooth second derivative from integer-valued data with median value 1.

- **RC-OFI**: `OFI / near_depth` where `near_depth = sum(vol_bid[1:2]) + sum(vol_ask[1:2])`. At the median, near_depth = 4 (1+1+1+1). The division by 4 is just rescaling OFI by a near-constant. When depth is this thin, the denominator doesn't vary enough to differentiate "thin book + high OFI" from "thick book + high OFI."

**Required response**: Run the Gate Zero diagnostic on TXFD6 AND TMFD6 L5 data BEFORE this survey is considered actionable. Report: (a) distribution of L2-L5 volumes per tick, (b) fraction of ticks with L2 vol > 2 and L3 vol > 2, (c) coefficient of variation of L2-L3 depth within sessions. If the median remains at 1 lot, all three candidates should be killed without prototyping.

---

### Challenge 2: The Bechler & Ludkovski (2017) contradiction claim is apples-to-oranges

The survey's strongest argument (Section 5) is that Bechler & Ludkovski (2017) "directly contradicts" R15's finding that L3-L5 add noise. The survey offers three possible resolutions (timescale, feature construction, market structure) but does not commit to which one applies -- it defers to Gate Zero.

**This is not a contradiction. It is a completely different market.**

Bechler & Ludkovski study **6 large-tick Nasdaq stocks** (INTC, MSFT, AAPL, GOOG, CSCO, AMZN). These are among the most liquid equities in the world:

- MSFT average daily volume: ~30M shares (2017 era)
- L1-L5 depth on Nasdaq large-tick stocks: typically **hundreds to thousands of lots** per level
- Tick size: $0.01 on stocks worth $30-180, creating genuine multi-level depth structure
- Participant mix: institutional HFTs, electronic market makers, algorithmic execution

TXFD6/TMFD6:
- Average daily volume: ~180K contracts (TXFD6) / ~60K contracts (TMFD6)
- L1-L5 depth: **median 1 lot per level** (verified above)
- Tick size: 1 point on a ~23,000 point index -- extremely granular relative to price
- Participant mix: retail-heavy (especially TMFD6), domestic proprietary traders

The survey claims the resolution "may be" timescale or feature construction. But the binding constraint is (3) -- **market structure**. A Nasdaq blue-chip with 500 lots at each of 5 levels has a rich, continuous depth profile where shape, slope, and convexity are well-defined mathematical objects. A TAIFEX futures contract with 1 lot at each of 5 levels has a sparse, integer-valued depth profile where "shape" is noise.

**Required response**: The Researcher must either (a) find a paper demonstrating multi-level depth predictive power on a market with comparable liquidity to TXFD6/TMFD6 (median depth ~1-3 lots), or (b) explicitly acknowledge that the Bechler & Ludkovski result may not transfer and reduce the confidence level of all three candidates accordingly. Simply deferring to Gate Zero is insufficient -- the survey's priority ordering (A > C > B) is based on Bechler & Ludkovski's relevance, which is unestablished.

---

### Challenge 3: DCI "convexity" is misnamed and trivially degenerate

The survey derives:
```
bid_convexity = cum_bid[2] - 2*cum_bid[1] + cum_bid[0]
             = vol_bid[3] - vol_bid[2]
```

The survey correctly identifies this simplification but does not grapple with its implications. The "Depth Convexity Index" is just `(vol[3] - vol[2])_bid - (vol[3] - vol[2])_ask`, normalized. This is:

1. **Not convexity in any meaningful sense.** Convexity (second derivative) requires at least 4 points to compute a finite difference that captures curvature. With 3 cumulative depth values, the "second difference" degenerates to a first difference of incremental volumes. This is a **depth change rate**, not curvature.

2. **The formula uses L2 and L3 only** (the difference vol[3]-vol[2]). L1 drops out entirely. This means DCI ignores the most informative level (L1, which has 5-8x more IC per R15) and depends entirely on the noisiest levels (L2-L3, which have median volume of 1 lot each). The signal literally discards the best data and uses the worst.

3. **On TXFD6, vol[3]-vol[2] is drawn from {-1, 0, 1} roughly 80% of the time** (since both levels have median 1 lot). The DCI signal is effectively ternary noise.

**Required response**: Either (a) extend the formula to use L1-L5 (requiring 5 data points for a genuine second derivative), or (b) rename the signal to acknowledge it measures L2-L3 depth difference, not convexity, and explain why this specific level pairing is theoretically motivated when L1 is excluded.

---

### Challenge 4: The survey is premature -- Gate Zero should come BEFORE the literature review

The survey concludes with a Gate Zero diagnostic (Section 4.3) that checks:
1. L2/L3 volume presence rate
2. L2/L3 volume stability
3. DWSA vs depth_imbalance correlation
4. Depth profile shape

**All four of these checks are data queries that take 15 minutes to run.** They require zero literature review, zero signal construction, zero theoretical framework. They are purely empirical questions about whether TXFD6/TMFD6's order book has enough L2-L5 depth to support ANY depth-shape signal.

Given that:
- R15 found "L1 dominates; L3-L5 add noise" (TXFD6)
- R18 found "L2-L5 adds nothing (L1 alone better)" (TXFD6 + 2330)
- R20 found median vol = 1 lot at all levels (TXFD6)
- R20 Direction 2 (Book Pressure Gradient) was killed with explicit note "R15 established that depth LEVEL information at L3-L5 is noise on TXFD6"

...the overwhelming prior evidence says Gate Zero will fail. Running it first would have either (a) saved the effort of writing this survey, or (b) provided the empirical foundation needed to credibly argue that R22 is different from R11/R15/R20.

**This is a process concern, not a technical one.** The survey's quality is high, but its sequencing is backwards. Literature reviews should follow data feasibility checks, not precede them.

**Required response**: Run Gate Zero immediately. If L2-L3 volume presence rate is < 70% or median L2 volume < 2 lots, terminate all three candidates and redirect effort to non-LOB-depth alpha sources (as recommended by R17's conclusion: "L1 microstructure EXHAUSTED").

---

## 3. Kill Criteria Review

### Missing kill criteria

1. **No TMFD6-specific volume floor.** All volume statistics cited in the survey are from TXFD6. TMFD6 has ~1/3 the daily volume. If TXFD6 has median 1 lot per level, TMFD6 likely has median 0-1 lots. The kill criteria reference "ticks with non-zero L2+L3 volume" but do not specify a minimum volume LEVEL (e.g., "median L2 vol >= 3 lots").

2. **No cross-round consistency gate.** The survey should explicitly state: "If Gate Zero confirms the findings of R15/R18/R20 (L2-L5 noise on TXFD6), all candidates are killed regardless of individual kill thresholds." Currently, a candidate could technically pass its individual K1-K5 while the overall L5 feasibility question remains negative.

3. **No degenerate signal check for DCI.** K5 checks L3 volume SNR but does not check for the **discrete/ternary** problem. A signal that takes values {-1, 0, 1} most of the time can technically have a non-zero IC and pass SNR checks while being practically untradeable due to quantization noise.

### Threshold objections

- **DWSA K1 (r > 0.60)**: Too lenient. R15 found gravity center at r=0.70 with depth_imbalance. If DWSA is at r=0.55, it would pass K1 but still be largely redundant. Recommend tightening to r > 0.40, consistent with the 20% incremental IC requirement in K5.

- **DCI K2 (|IC| < 0.010 at 30s)**: This is the weakest kill threshold across all candidates. IC=0.010 at 30s horizon is far below the cost breakeven (IC > 0.030 per R17 analysis). Even if DCI passes K2, it cannot clear cost breakeven. This threshold should be 0.025 minimum.

- **RC-OFI K3 (r > 0.90)**: Appropriate but may be too generous. If OFI/depth correlates at r=0.85 with raw OFI, the remaining 15% variance is unlikely to be tradeable signal rather than noise.

---

## 4. Candidate Ranking Assessment

**Disagree with A > C > B.**

The survey ranks based on theoretical backing and computational simplicity. But given the empirical evidence from R11/R15/R18/R20, the ranking should be based on **probability of surviving Gate Zero**:

- **Candidate B (RC-OFI)** uses L1+L2 only and has the least dependence on thin L3-L5 data. The `OFI / near_depth` construction is the most likely to produce a non-degenerate signal because it requires only L1-L2, where volumes are occasionally meaningful (P90 = 3). It is also the most theoretically grounded (Cont et al. 2014 is a canonical result, not a niche finding).

- **Candidate A (DWSA)** requires L1-L3 and measures concentration ratios. With median 1 lot at each level, concentration is nearly always 1/3. This is unlikely to survive Gate Zero unless TMFD6 depth is substantially different from TXFD6.

- **Candidate C (DCI)** requires the DIFFERENCE between L2 and L3 volumes. This is the most data-demanding candidate and the most likely to be degenerate (ternary signal on thin books). It should be ranked last.

**Recommended ranking**: B > A > C, with the caveat that all three have a high probability (~70-80%) of being killed at Gate Zero based on prior evidence.

---

## 5. Recommendation

### Immediate action (before any further survey work):

1. **Run Gate Zero NOW** on both TXFD6 and TMFD6 L5 data. This is a 15-minute data query, not a research project. The four checks in Section 4.3 of the survey are exactly right -- just do them before spending more time on literature.

2. **Add TMFD6 L5 volume statistics** to Gate Zero. TMFD6 is the primary target for CBS/OpMM but has never had its L5 depth characterized. If TMFD6 L2-L5 is as thin as TXFD6 (likely thinner given 1/3 the volume), all three candidates are dead.

3. **If Gate Zero passes** (L2 median vol >= 2, L3 presence rate >= 60%), proceed with B (RC-OFI) first -- it has the lowest data requirements and the strongest canonical backing.

4. **If Gate Zero fails** (which the prior evidence strongly predicts), formally close the "LOB depth shape" research direction. The platform has now attempted L2-L5 alpha in R11, R15, R18, R20, and R22. Five rounds of negative results on the same market constitute a conclusive finding: **TAIFEX futures L2-L5 depth does not contain tradeable information at any horizon or through any transformation.** Document this as a structural conclusion and redirect research to the directions identified in R17 (calendar patterns, TXO options, cross-asset macro).

---

## 6. Summary of Challenges

| # | Challenge | Severity | Status |
|---|-----------|----------|--------|
| C1 | TXFD6 L5 median vol = 1 lot at all levels; signals degenerate to discrete noise | CRITICAL | Unresolved -- requires Gate Zero |
| C2 | Bechler & Ludkovski (Nasdaq blue chips) is apples-to-oranges vs TAIFEX futures | HIGH | Unresolved -- need comparable-liquidity citation or confidence downgrade |
| C3 | DCI "convexity" is misnamed; formula uses only L2-L3 and discards L1 | MEDIUM | Unresolved -- formula redesign or rename needed |
| C4 | Survey sequencing is backwards; Gate Zero should precede literature review | MEDIUM | Process issue -- run Gate Zero immediately |

**Unresolved CRITICAL challenges = REJECT.**

The survey may be re-submitted after Gate Zero results are available. If Gate Zero passes, the survey's analysis becomes actionable and the remaining challenges (C2, C3) can be resolved in the response. If Gate Zero fails, the survey should be archived as negative evidence for the structural conclusion above.

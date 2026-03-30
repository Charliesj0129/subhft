# Round 20 Stage 1: L2-L5 Depth Strategy Survey — TMFD6

**Date**: 2026-03-27
**Researcher**: Alpha Research Agent (R20)
**Scope**: Alpha strategies that specifically leverage L2-L5 book depth on TMFD6
**Pivot**: From L1-only exploration (earlier R20 survey) to L5-specific directions

---

## Executive Summary

R12-R19 used L1 data almost exclusively. ClickHouse stores full L1-L5 depth, and L5 export is now available. This survey explores 6 depth-specific directions that go beyond what R11 (MLOFI gradient), R15 (LOB kinetic energy), and R18 (MLOFI microprice) tested.

**Critical prior findings that constrain this survey:**

| Round | What was tested with depth | Result |
|-------|---------------------------|--------|
| R11 | MLOFI gradient (shallow vs deep OFI momentum) on 2330 | IC=-0.105 (inverted sign), Gate C FAIL |
| R15 | LOB kinetic energy, gravity center on TXFD6 L5 | IC too weak. **L1 has 5-8x more IC than L2-L5.** Momentum collinear with depth_imbalance (r=0.70) |
| R18 | MLOFI microprice correction on 2330 + TXFD6 | 2330: L1-only IC (+0.217) > full MLOFI (+0.206). L2-L5-only IC = +0.029. TXFD6: zero IC. **Detrended IC negative — MLOFI is trend-following, not alpha.** |
| FE v2 | `deep_depth_momentum_x1000` [20] | L2-L5 depth net delta with EMA crossover. Currently deployed. IC not independently reported for TMFD6. |

**The prior evidence is strongly pessimistic for L2-L5 on TXFD6.** However, TMFD6 is UNTESTED with L5 data and has different microstructure (smaller contract, more retail, tighter spread in front-month). The question is whether TMFD6's depth structure contains information that TXFD6's did not.

### Data Available

- **ClickHouse**: ~58 days TMFD6 with L1-L5 arrays (`bids_price[5]`, `bids_vol[5]`, `asks_price[5]`, `asks_vol[5]`)
- **L5 .npy**: TMFD6 needs extraction (run `ch_batch_export.py --symbols TMFD6 --formats l5 --concat`)
- **TXFD6 L5**: 2.17M ticks, available at `research/data/l5/TXFD6_l5.npy`
- **2330 L5**: Available at `research/data/l5/2330_l5.npy`
- **L5 dtype**: `(timestamp_ns, bids_price[5], bids_vol[5], asks_price[5], asks_vol[5])` — prices scaled x1000000

---

## Direction 1: Depth Withdrawal Asymmetry (DWA)

### Concept

Track the RATE OF CHANGE of depth at L2-L5 levels SEPARATELY on bid and ask sides. When depth is being withdrawn asymmetrically (e.g., ask-side L2-L5 depth declining while bid-side stable), this signals informed traders pulling quotes before a move. Different from MLDM (FE v2 #20) which aggregates ALL levels into a single net delta.

### Key Literature

**Biais, Hillion & Spatt (1995)** — "An Empirical Analysis of the Limit Order Book and the Order Flow in the Paris Bourse." Journal of Finance 50(5), 1655-1689.
- Foundational study of full LOB dynamics on Paris Bourse
- Documents that order flow at different price levels carries different information
- Depth additions at L1 are mainly uninformed; depth removals at L2-L5 are more informative
- Key finding: cancellations at deep levels precede price moves by 2-10 seconds

**Cao, Hansch & Wang (2009)** — "The Informational Content of an Open Limit-Order Book." Journal of Futures Markets 29(1), 16-41.
- Studies the full LOB of ASX (Australian Stock Exchange) futures
- L2-L5 depth contributes to price discovery BEYOND L1
- But: the contribution decays rapidly with depth — L2 adds ~15% explanatory power, L3-L5 add < 5% combined
- Key insight: the CHANGE in depth (flow) at L2-L3 is more informative than the LEVEL

**Wang (2025)** — "Forecasting Liquidity Withdrawal with ML Models." arXiv:2509.22985.
- Constructs a Liquidity Withdrawal Index: LWI = cancellations / (standing_depth + new_additions)
- Asymmetric LWI (bid-side vs ask-side) predicts short-term price direction
- ML models (gradient boosted trees) achieve modest but significant predictive power
- Key limitation: requires order-level data (submissions, cancellations). We have snapshots, not order-level.

### The Specific Signal

**Proposed features**:
1. `dwa_bid = sum(delta_bid_vol[L2:L5])` — total bid depth change at L2-L5 (one tick to next)
2. `dwa_ask = sum(delta_ask_vol[L2:L5])` — total ask depth change at L2-L5
3. `dwa_asym = dwa_bid - dwa_ask` — asymmetry: positive = ask depth withdrawing faster (bearish)
4. `dwa_asym_ema = EMA(dwa_asym, span=16)` — smoothed asymmetry

**Trading logic**: When `dwa_asym_ema` is strongly positive (ask depth withdrawing), price is likely to rise. Enter long. Vice versa for negative.

### Critical Difference from Prior Work

| Feature | What it measures | Level aggregation | Tested on TMFD6? |
|---------|-----------------|-------------------|-------------------|
| MLDM (FE v2 #20) | Net delta of ALL L2-L5 depth (bid - ask) | Aggregated sum | In production, IC unknown |
| R15 KE momentum | Static depth * distance^2, normalized | Weighted by distance | TXFD6 only |
| R11 MLOFI gradient | Shallow OFI vs deep OFI momentum | Two-group split | 2330 only |
| **DWA (proposed)** | **RATE of depth change, bid vs ask SEPARATELY** | **Per-side, then asymmetry** | **No** |

DWA is closest to MLDM but differs in: (a) per-side decomposition (not just net), (b) pure rate-of-change (not EMA crossover), (c) has not been tested on TMFD6.

### Honest Assessment

**The elephant in the room**: R15 showed L1 has 5-8x more IC than L2-L5 on TXFD6. If the same holds for TMFD6, DWA will have negligible signal. The question is: does TMFD6's different participant mix (more retail, less HFT) mean that L2-L5 carries MORE information because institutional orders sit at deeper levels?

**TMFD6-specific hypothesis**: On TMFD6 (mini contract, retail-heavy), L2-L5 depth may be dominated by retail limit orders. Retail flow is generally UNINFORMED. If so, depth withdrawal at L2-L5 is noise (order aging and revision), not informed signal. This would CONFIRM the TXFD6 finding (L1 >> L2-L5).

**Counter-hypothesis**: TMFD6's tighter front-month spread (3 pts) means L2-L5 levels are very close to mid (perhaps 4-8 pts away). At such close distances, depth levels may be more "competitive" than on TXFD6 where L2-L5 can be 50-100 pts from mid. Closer levels = more informative depth.

**This must be empirically resolved.** The first step is to extract TMFD6 L5 data and check the typical L2-L5 price distances. If L2-L5 are clustered within 5-10 pts of mid, the depth is "competitive" and potentially informative. If they're 50+ pts away, it's noise.

---

## Direction 2: Book Pressure Gradient (BPG)

### Concept

Compute the "slope" of depth across levels. Instead of aggregating all levels, measure how depth CHANGES from L1 to L5. A "convex" book (increasing depth at deeper levels) signals resilience — lots of liquidity behind the top of book. A "concave" book (decreasing depth) signals fragility — the top of book is unsupported.

### Key Literature

**Naes & Skjeltorp (2006)** — "Order Book Characteristics and the Volume-Volatility Relation: Empirical Evidence from a Limit Order Market." Journal of Financial Markets 9(4), 408-432.
- Studies the relationship between LOB shape and future volatility
- "Steep" books (depth concentrated at L1) precede low volatility
- "Flat" books (depth spread across levels) precede high volatility
- Key finding: book shape predicts 5-minute volatility with R-squared ~ 0.05-0.10

**Cont, Stoikov & Talreja (2010)** — "A Stochastic Model for Order Book Dynamics." Operations Research 58(3), 549-563.
- Models the LOB as a system of interacting queues at each price level
- Order arrival and cancellation rates DIFFER by level — deeper levels have lower arrival rates but also lower cancellation rates
- The ratio of arrival-to-cancellation rates determines long-run depth profile

**Hortacsu & Syverson (2004)** — "Product Differentiation, Search Costs, and Competition in the Mutual Fund Industry." Quarterly Journal of Economics 119(2).
- Not directly LOB, but establishes that the SHAPE of a distribution (not just its mean) carries information
- Application: the shape of the depth profile (convex vs concave) encodes information about market maker confidence

### The Specific Signal

**Proposed features**:
1. `bid_gradient = (bid_vol[L3] - bid_vol[L1]) / 2` — depth slope on bid side (positive = convex/resilient)
2. `ask_gradient = (ask_vol[L3] - ask_vol[L1]) / 2` — depth slope on ask side
3. `gradient_asym = bid_gradient - ask_gradient` — which side is more resilient?
4. `book_shape = total_deep_vol / L1_vol` — ratio of L2-L5 depth to L1 depth (convex = high ratio)

**Trading logic**:
- High `gradient_asym` (bid side more resilient than ask): price support exists, bullish
- High `book_shape` with low `spread`: stable market, good for spread capture (OpMM enhancement)
- Low `book_shape`: fragile book, avoid maker positions

### Critical Difference from Prior Work

R15's KE used distance-SQUARED weighting (emphasizes far levels). BPG uses a simple LINEAR gradient (L3 - L1). R15's gravity center is the depth-weighted average distance — BPG is the depth SLOPE. These are related but not identical.

**Collinearity concern**: BPG's `gradient_asym` may correlate with `depth_imbalance` (FE v1 feature). R15 found that KE momentum was r=0.70 with depth_imbalance. BPG needs to demonstrate independence.

### Honest Assessment

**Strengths**: Simple, interpretable, computable in O(1) per tick, naturally extends OpMM (quote only when book is resilient).

**Weaknesses**:
- R15 established that depth LEVEL information at L3-L5 is noise on TXFD6. If the absolute depth is uninformative, its GRADIENT is likely also uninformative.
- Book shape varies significantly with spread regime (wide spread = few levels populated = noisy gradient).
- March front-month TMFD6 (3pt spread) may have very few populated levels beyond L1. If L2-L5 are sparse, the gradient is undefined.
- Collinearity with depth_imbalance is likely high.

**Data prerequisite**: Must first check whether TMFD6 front-month L2-L5 levels are populated. If L3-L5 are typically empty (vol=0), this direction is dead before starting.

---

## Direction 3: Depth Resilience After Trade Impact

### Concept

Measure how quickly depth RECOVERS at L1-L3 after a trade depletes it. Fast recovery = liquidity is being replenished by uninformed market makers (safe for makers). Slow recovery = the trade consumed genuine liquidity and the book is fragile (unsafe).

### Key Literature

**Obizhaeva & Wang (2013)** — "Optimal Trading Strategy and Supply/Demand Dynamics." Journal of Financial Markets 16(1), 1-32.
- Models LOB resilience as a key parameter in optimal execution
- Resilience = rate at which depth refills after a trade
- Higher resilience instruments allow more aggressive execution
- The resilience parameter is measurable from historical data

**Bouchaud, Farmer & Lillo (2009)** — "How Markets Slowly Digest Changes in Supply and Demand." Handbook of Financial Markets.
- Documents that after a large trade, the LOB refills gradually over 10-100 seconds
- The refill pattern is ASYMMETRIC: the depleted side refills slowly, the opposite side withdraws quickly
- This "latent liquidity" reveals the market's true supply/demand schedule
- Application: resilience speed as a regime indicator

**Degryse, de Jong & van Kervel (2015)** — "The Impact of Dark Trading and Visible Fragmentation on Market Quality." Review of Finance 19(4), 1587-1622.
- Studies how depth recovery differs by venue and instrument
- Key finding: faster recovery = more competitive market making = lower adverse selection
- Application: resilience speed as an adverse selection proxy

### The Specific Signal

**Proposed features**:
1. Detect "depth depletion events": L1 bid_vol drops by >= 50% in one tick (trade hit bid side)
2. Measure `recovery_time_ms`: time until bid_vol returns to >= 80% of pre-depletion level
3. `resilience_ratio = depth_before / recovery_time_ms` — combines magnitude and speed
4. Rolling `resilience_ema` over last 20 depletion events

**Trading logic**:
- High resilience (fast recovery): market is healthy, safe for maker strategies (OpMM)
- Low resilience (slow recovery): informed flow regime, avoid maker positions
- Resilience CHANGE (dropping from high to low): regime transition, potential CBS trigger

### Honest Assessment

**Strengths**:
- Entirely different from static depth signals (MLOFI, depth_imbalance, etc.)
- Measures a DYNAMIC property (recovery speed) not a STATIC snapshot
- Theoretically well-grounded (Obizhaeva & Wang 2013)
- Natural use case: OpMM conditional gate (only quote when resilience is high)
- Not previously tested on any instrument in R12-R19

**Weaknesses**:
- Requires identifying "depletion events" from L1 snapshots. At 125ms tick interval, we see the RESULT of depletion, not the event itself. If recovery happens between ticks (< 125ms), we miss it entirely.
- TMFD6 L1 depth is typically 1-5 lots. A depletion of "50%" could be 1 lot dropping to 0 — extremely noisy.
- Need sufficient depletion events per day for statistical reliability. If TMFD6 has only 50-100 visible depletions/day, the rolling window is very coarse.
- Recovery time measurement is quantized to tick interval (125ms). If most recovery happens within 1-3 ticks, we get very coarse buckets.

**TMFD6-specific concern**: With a median depth of 4.1 lots at L1 (R18), and 125ms tick interval, many "depletions" will be 4 lots → 2 lots → 4 lots — all happening within 1-2 ticks. The signal may be too quantized to be useful.

---

## Direction 4: Level-Weighted Microprice vs L1 Microprice Divergence

### Concept

Compute two microprice estimates:
1. `micro_L1 = imbalance_L1 * ask + (1 - imbalance_L1) * bid` — standard L1 weighted mid
2. `micro_L5 = sum(w_i * imbalance_Li * mid_Li)` — weighted average across all 5 levels

The DIFFERENCE `micro_L5 - micro_L1` is a "depth correction" signal. When deep levels disagree with L1, the deep level may be more informative about the true fair value.

### Key Literature

**Stoikov (2018)** — "The micro-price: a high-frequency estimator of future prices." Quantitative Finance 18(12).
- Defines L1 microprice as the optimal fair value estimator given L1 state
- Shows L1 microprice outperforms mid-price and VWAP mid for short-term prediction
- Limitation: ignores deeper levels entirely

**Xu, Gould & Howison (2019)** — "Multi-Level Order-Flow Imbalance in a Limit Order Book." arXiv:1907.06230.
- MLOFI regression coefficients give the optimal weighting for each level's contribution
- PCA first component weights decay geometrically with depth
- Application: the regression beta at each level is the level's "information weight"

**Berild, Lei & Granmo (2024)** — "High Resolution Microprice Estimates from LOB Data." arXiv:2411.13594.
- Extends Stoikov by adding L2-L5 "error correction" to L1 microprice
- Shows marginal improvement on Nasdaq data
- Key finding: the error correction term is small but statistically significant

### Critical Prior Finding

**R18 explicitly tested this on 2330 and TXFD6:**
- 2330: L1-only microprice IC (+0.217) was HIGHER than full MLOFI microprice IC (+0.206)
- L2-L5-only IC = +0.029 (marginal, ~14% of L1)
- On TXFD6: zero IC for any microprice variant
- **Detrended IC was NEGATIVE** — the entire microprice signal was trend-following

This is the most directly tested direction in prior work. The result was clear: L2-L5 adds negligible information to microprice on both 2330 and TXFD6.

### Honest Assessment

**This direction is nearly dead on arrival for TMFD6.** The prior evidence on both equity (2330) and large-contract futures (TXFD6) shows L2-L5 adds < 15% to microprice, and the detrended analysis reveals even L1 microprice is trend-following rather than microstructure alpha. There is no theoretical reason TMFD6 would be different — if anything, TMFD6's thinner book means L2-L5 are MORE noisy (lower depth, wider price gaps between levels).

**Only pursue if**: TMFD6 L5 data shows a qualitatively different book structure (e.g., significantly deeper at L2-L5 than TXFD6, or very tight level spacing). Otherwise, kill immediately.

---

## Direction 5: Depth-Shape Regime Classification

### Concept

Classify the current book state into discrete "shapes" and test whether the book shape predicts future price behavior. Shapes might include:
- **Symmetric/balanced**: similar depth on both sides at all levels
- **Bid-heavy/Ask-heavy**: asymmetric depth (captured partly by depth_imbalance)
- **Cliff**: deep at L1, empty at L2-L5 (fragile)
- **Pyramid**: increasing depth at deeper levels (resilient)
- **Sparse**: very low depth across all levels (illiquid)

### Key Literature

**Gould, Porter, Williams, McDonald, Fenn & Howison (2013)** — "Limit Order Books." Quantitative Finance 13(11), 1709-1748.
- Comprehensive review of LOB modeling. Documents empirical LOB shapes across markets.
- Typical shape: "hump" at L1-L3, tailing off at L4-L5
- Shape varies by instrument, time of day, and market conditions

**Bouchaud, Mezard & Potters (2002)** — "Statistical Properties of Stock Order Books: Empirical Results and Models." Quantitative Finance 2(4), 251-256.
- Documents the average shape of the LOB is "exponential" — depth increases away from mid-price
- Deviations from this average shape are informative
- Application: measure deviation from average shape as a signal

**Muni Toke & Yoshida (2017)** — "Modelling intensities of order flows in a limit order book." Quantitative Finance 17(5), 683-701.
- Models order arrival, cancellation, and market order intensities at each price level
- The relative intensities determine the equilibrium LOB shape
- Application: deviations from equilibrium shape signal non-equilibrium (transitional) states

### Honest Assessment

**Strengths**: Shape classification is interpretable and could serve as a regime indicator for existing strategies. A "cliff" shape might signal imminent fragility (avoid maker positions). A "pyramid" shape might signal stability.

**Weaknesses**:
- Shape classification requires several discrete categories — with 20 days and 5-shape classification, we get ~4 days per shape. Insufficient for statistical analysis.
- R15 found that the MOST informative depth feature (gravity center, IC=-0.025) was an INVERTED predictor — and even that was too weak. Shape classification uses the same information (depth profile) with less granularity.
- TMFD6 front-month with 3pt spread may have very few populated levels, making shape classification degenerate (most ticks = "sparse" or "L1-only").
- Collinearity with depth_imbalance and spread is likely high.

---

## Direction 6: Cross-Level OFI Divergence (L1 vs Deep)

### Concept

Compute OFI at L1 and OFI at L2-L5 SEPARATELY. When they diverge (L1 OFI positive but deep OFI negative, or vice versa), the divergence signals conflicting information:
- **L1 positive, deep negative**: Surface buying but institutional selling (withdrawal of support at depth). Bearish divergence.
- **L1 negative, deep positive**: Surface selling but depth accumulating (institutional buying at lower prices). Bullish divergence.

### Key Literature

**Xu, Gould & Howison (2019)** — arXiv:1907.06230. (Already cited.)
- PCA on MLOFI: PC1 is the "average flow" (co-directional across levels). PC2 is the "depth contrast" (L1 vs L2-L5 divergence).
- PC2 explains 10-15% of price variance BEYOND PC1.
- Application: PC2 (the divergence) is a separate signal from PC1 (the aggregate).

**Cont, Cucuringu & Zhang (2021)** — arXiv:2112.13213. (Already cited.)
- Cross-level OFI contributions are NOT redundant — each level adds incremental information
- But: the incremental R-squared from deeper levels is small (< 5% for L4-L5)

### Critical Prior Finding

**R11 tested "shallow vs deep OFI momentum" (MLOFI gradient) on 2330:**
- Result: IC = -0.105 (INVERTED — deep momentum predicts the OPPOSITE direction)
- Gate C FAIL because even with flipped sign, the edge was < cost
- The signal exists but in the WRONG direction — deep depth is contrarian, not confirmatory

**R15 per-level IC decomposition on TXFD6:**
- L1: IC = +0.017
- L2: IC = +0.003
- L3: IC = +0.002
- L4: IC = -0.001
- L5: IC = +0.000

**Crucially**: L2-L5 have essentially ZERO independent IC on TXFD6.

### Honest Assessment

The divergence concept (L1 vs deep OFI) is theoretically motivated by Xu et al.'s PC2 finding. However:
- R11 found the divergence on 2330 but it was INVERTED (deep predicts reversal)
- R15 found L2-L5 have zero IC on TXFD6
- The signal is R11's MLOFI gradient under a different name
- Even if TMFD6 shows divergence, the cost structure (3.92 pts RT) likely exceeds any edge from a secondary PCA component

**This is the closest to a rehash of R11.** The only genuine novelty would be if TMFD6 shows qualitatively different L2-L5 behavior than 2330 or TXFD6.

---

## Pre-Prototype Gate Zero: TMFD6 L5 Data Diagnostic

Before selecting candidates, we MUST answer a fundamental question: **What does TMFD6's L5 book actually look like?**

### Required diagnostics (from ClickHouse or newly-extracted L5 .npy):

1. **Level population rate**: What fraction of ticks have non-zero depth at L2, L3, L4, L5? If L3-L5 are empty > 50% of the time, most L5 directions are dead.

2. **Level price distances**: How far are L2-L5 from mid-price in points? If L2 is 1 pt from mid and L5 is 3 pts, levels are "competitive" (informative). If L2 is 10 pts and L5 is 50 pts, levels are "noise" (mostly stale orders).

3. **Level depth magnitudes**: Average depth at each level. If L1 = 4 lots and L5 = 0.2 lots, the deep book is essentially empty.

4. **Per-level IC**: Same analysis as R15 DC-1 but on TMFD6. Compute depth-delta IC at each level for 1s, 5s, 30s forward returns. This directly answers whether L2-L5 carry signal on TMFD6.

5. **March vs Jan/Feb depth structure**: Does front-month TMFD6 have different L5 characteristics than far-month? (March = liquid, tight spread. Jan/Feb = illiquid, wide spread.)

**This diagnostic takes ~2 hours and uses existing L5 data from ClickHouse. Its results determine which candidates proceed.**

---

## Candidate Selection

### Summary Table (Pre-Diagnostic)

| # | Direction | Novelty vs R11/R15/R18 | Prior Evidence | Feasible? | GO/NO-GO |
|---|-----------|----------------------|----------------|-----------|----------|
| 1 | Depth Withdrawal Asymmetry (DWA) | MEDIUM (closest to MLDM but per-side) | MLDM deployed, IC unknown on TMFD6 | YES | **CONDITIONAL GO** (pending Gate Zero) |
| 2 | Book Pressure Gradient (BPG) | MEDIUM (new formulation of depth profile) | R15: L3-L5 IC=0 on TXFD6 | YES | **CONDITIONAL GO** (pending Gate Zero) |
| 3 | Depth Resilience After Trade | HIGH (dynamic, not static — untested) | None directly | PARTIAL (125ms quantization concern) | **GO** (most novel) |
| 4 | L5 vs L1 Microprice Divergence | LOW (R18 explicitly tested) | R18: L2-L5 adds < 15%, detrended IC negative | YES | **NO-GO** (R18 killed this) |
| 5 | Depth-Shape Regime | MEDIUM (new framing) | R15: depth profile IC weak | YES | **NO-GO** (N=20, shape degeneracy) |
| 6 | Cross-Level OFI Divergence | LOW (R11 MLOFI gradient renamed) | R11: IC=-0.105 inverted, Gate C FAIL | YES | **NO-GO** (R11 rehash) |

### Candidate A: Depth Resilience After Trade Impact (Direction 3) — **GO**

**Rationale**: The only direction that measures a DYNAMIC property (recovery speed) rather than a static snapshot. Not tested in any prior round. Theoretically grounded in optimal execution literature (Obizhaeva & Wang 2013). Natural use: OpMM gate (trade only when resilience is high = low adverse selection).

**Risk**: 125ms tick quantization may make recovery measurement too coarse. Must verify that depletion events are visible and recovery takes multiple ticks.

**Implementation plan**:
1. Extract TMFD6 L5 data (`ch_batch_export.py`)
2. Identify L1 depletion events (>= 50% qty drop in one tick)
3. Measure recovery time (ticks until 80% restoration)
4. Compute rolling resilience feature
5. Test: (a) standalone IC for return prediction, (b) OpMM P&L conditioning by resilience quartile
6. Kill criterion: If < 50 depletion events/day in March, the signal is too sparse. If resilience quartile conditioning does not separate OpMM P&L by > 5 ppt, kill.

**Data**: L5 .npy (need to extract TMFD6). ~100 LOC prototype.

### Candidate B: Depth Withdrawal Asymmetry (Direction 1) — **CONDITIONAL GO**

**Rationale**: Per-side depth withdrawal is theoretically different from MLDM's net aggregation. The BID-SIDE vs ASK-SIDE decomposition may reveal information that the net signal washes out. But strongly conditional on Gate Zero showing L2-L5 are populated and informative on TMFD6.

**Risk**: May be redundant with MLDM (FE v2 #20). Must measure correlation between DWA and MLDM. If |rho| > 0.60, kill.

**Implementation plan**:
1. Gate Zero diagnostic first — check L5 population and per-level IC
2. If L2-L5 have IC > 0.005 on TMFD6 → compute DWA
3. Measure DWA correlation with MLDM
4. If orthogonal → test CBS/OpMM conditioning
5. Kill criterion: |rho| > 0.60 with MLDM, or per-level IC < 0.005 at all horizons

**Data**: L5 .npy (need extraction). ~80 LOC prototype.

### Candidate C: Book Pressure Gradient (Direction 2) — **CONDITIONAL GO**

**Rationale**: Simple signal (depth slope across levels) with clear interpretation (convex = resilient). Could enhance OpMM by gating on book quality. But strongly conditional on Gate Zero showing populated levels.

**Risk**: Collinearity with depth_imbalance (R15 found r=0.70 for KE momentum). Must check.

**Implementation plan**:
1. Gate Zero diagnostic first
2. If L2-L3 are populated on TMFD6 → compute gradient
3. Measure gradient correlation with depth_imbalance
4. Kill criterion: |rho| > 0.60 with depth_imbalance, or IC < 0.005

**Data**: L5 .npy. ~50 LOC prototype.

---

## Gate Zero: Critical Prerequisite

**ALL three candidates are conditional on Gate Zero diagnostic results.**

If Gate Zero shows:
- L3-L5 unpopulated > 50% of time in March → Kill B and C, proceed with A only (which only needs L1 depth changes)
- Per-level IC at L2-L5 on TMFD6 < 0.003 at all horizons → Kill B and C (confirming R15 finding generalizes)
- L1 depletion events < 50/day in March → Kill A (insufficient data)

If ALL Gate Zero results are negative → R20 L5 exploration is concluded negative. The honest finding is that L2-L5 data on TMFD6 does not contain exploitable signal, consistent with R15's TXFD6 finding. This would strengthen the case for the L1-focused data infrastructure pivot recommended by the challenger.

---

## Recommendations for Team Review

### For Challenger

1. **Direction 3 (Resilience)**: Is recovery-time measurement feasible at 125ms granularity? If median recovery time is 1-2 ticks, the signal degenerates into a binary (fast/slow). Challenge the quantization issue.
2. **Direction 1 (DWA)**: Is per-side decomposition genuinely different from MLDM's net delta? Mathematically, `dwa_bid - dwa_ask = MLDM` if MLDM is sum of (bid_delta - ask_delta). Challenge the independence claim.
3. **Overall**: Given R15's finding (L1 has 5-8x more IC), is L5 exploration justified at all, or is this sunk-cost reasoning ("we have L5 data, let's use it")?

### For Execution

1. **Gate Zero**: Can we run the 5-point diagnostic from ClickHouse directly without extracting .npy? This would save time.
2. **L5 extraction**: What's the estimated size and time for `ch_batch_export.py --symbols TMFD6 --formats l5 --concat`?
3. **Resilience feature**: Can a "depletion event detector" + "recovery timer" fit in FeatureEngine without violating the Allocator Law? (Needs state: `last_depletion_ts`, `pre_depletion_vol`, etc.)

---

## References

1. Biais, Hillion & Spatt (1995). "An Empirical Analysis of the Limit Order Book." J Finance 50(5).
2. Cao, Hansch & Wang (2009). "Informational Content of an Open Limit-Order Book." J Futures Markets 29(1).
3. Wang (2025). "Forecasting Liquidity Withdrawal with ML Models." arXiv:2509.22985.
4. Naes & Skjeltorp (2006). "Order Book Characteristics and Volume-Volatility." J Financial Markets 9(4).
5. Cont, Stoikov & Talreja (2010). "A Stochastic Model for Order Book Dynamics." Operations Research 58(3).
6. Obizhaeva & Wang (2013). "Optimal Trading Strategy and Supply/Demand Dynamics." J Financial Markets 16(1).
7. Bouchaud, Farmer & Lillo (2009). "How Markets Slowly Digest Changes." Handbook of Financial Markets.
8. Degryse, de Jong & van Kervel (2015). "Impact of Dark Trading." Review of Finance 19(4).
9. Stoikov (2018). "The micro-price." Quantitative Finance 18(12).
10. Xu, Gould & Howison (2019). "Multi-Level Order-Flow Imbalance." arXiv:1907.06230.
11. Berild, Lei & Granmo (2024). "High Resolution Microprice Estimates." arXiv:2411.13594.
12. Gould, Porter, Williams et al. (2013). "Limit Order Books." Quantitative Finance 13(11).
13. Bouchaud, Mezard & Potters (2002). "Statistical Properties of Stock Order Books." Quantitative Finance 2(4).
14. Muni Toke & Yoshida (2017). "Modelling intensities of order flows." Quantitative Finance 17(5).
15. Cont, Cucuringu & Zhang (2021). "Cross-Impact of Order Flow Imbalance." arXiv:2112.13213.

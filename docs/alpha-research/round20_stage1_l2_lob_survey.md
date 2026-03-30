# Round 20 — Stage 1 Literature Survey: L2 LOB Data-Driven Strategies

**Date**: 2026-03-27
**Researcher**: Claude (Researcher agent)
**Scope**: arXiv literature survey on Level 2+ (multi-level order book) data-driven profitable trading strategies

---

## 1. Research Context

### Available Data
| Dataset | Format | Rows | Depth | Notes |
|---------|--------|------|-------|-------|
| `research/data/l5/TXFD6_l5.npy` | L5 snapshots | 2.17M | 5-level | 10 trading days (Feb-Mar 2026), x10000 scaled |
| `research/data/l5/2330_l5.npy` | L5 snapshots | ~537K | 5-level | TSMC stock |
| `research/data/l5/2317_l5.npy` | L5 snapshots | ~786K | 5-level | Hon Hai stock |
| `research/data/l5_v2/` | L5 snapshots | varies | 5-level | 2330, 2317, TXFE6 (v2 format) |
| `research/data/raw/txfd6/*_l2.hftbt.npz` | L2 events | 3.37M/day | reconstructed | 4 days (Mar 19-24), **NOT true MBO** |
| L1 daily files | tick + bidask | extensive | L1 | TXFD6, TMFD6, 2330 — 40+ days |

### Critical Data Limitation
The L2 event data (`*_l2.hftbt.npz`) is **reconstructed from L5 snapshot diffs**, not true market-by-order (MBO) data:
- `order_id` = 0 everywhere (no real order tracking)
- No sell-side add/cancel events (only buy-side + trade fills)
- Equal ADD and CANCEL counts (snapshot reconstruction artifact)
- **Implication**: Any approach requiring order lifecycle tracking (placement -> modification -> cancellation -> fill) is NOT feasible with current data. Only approaches using L5 snapshot series or aggregated L2 features are viable.

### Prior Negative Results (R15-R19 Summary)
| Round | Approach | Result | Key Lesson |
|-------|----------|--------|------------|
| R15 | LOB KE/momentum | DEAD | L1 dominates; L3-L5 add noise on TXFD6. Depth asymmetry = reversal, not continuation |
| R16 | q-fin.TR microstructure | DEAD | Signal-horizon mismatch: signals work 5-15s, costs need 60s+ |
| R18 | MLOFI Microprice | DEAD | L2-L5 adds nothing over L1. Trend contamination (detrended IC mandatory) |
| R19 | MF Horizon Extension | DEAD | HF signals CANNOT extend to MF via math transforms. Physics problem |
| R13 | Bidirectional MM | DEAD | Queue priority bottleneck at 36ms RTT. MM unviable |
| TMFD6 OpMM | Market Making | DEAD | March spread < RT cost. MM structurally unprofitable |

### Trading Constraints
- **TXFD6**: RT cost ~2.0 bps (4 pts), median tick interval 125ms
- **TMFD6**: RT cost ~1.19 bps (3.92 pts)
- **2330**: RT cost ~6 bps (tax + commission)
- **Latency**: Shioaji sim RTT ~36ms (P95)
- **No maker rebates** (retail TAIFEX)
- **Detrended IC gate**: mandatory for all candidates

---

## 2. Literature Survey

### 2.1 Order Flow Event Decomposition & Clustering

**[ClusterLOB] Zhang et al. (2025)** -- arXiv:2504.20349
- **Method**: K-means++ clustering of MBO events using 6 time-dependent features -> 3 clusters (directional, opportunistic, market-making). OFI computed per-cluster as signal.
- **Results**: Cluster-specific OFI (especially "opportunistic" cluster) achieves higher Sharpe than aggregate OFI. Works across small/medium/large tick stocks on NASDAQ.
- **Key insight**: Decomposing OFI by participant type extracts stronger signal than aggregate OFI.
- **Limitation**: Requires true MBO data with order lifecycle. **NOT directly feasible with our reconstructed L2.**

**[OFI Decomposition] Cont, Cucuringu et al. (2023)** -- referenced in ClusterLOB
- **Method**: Client order flow segmentation by behavioral features, heterogeneity captured via prototype clustering.
- **Relevance**: Confirms that disaggregated OFI > aggregated OFI, but needs broker-level data.

**[Trade Flow Decomposition / COI] Lu, Reinert & Cucuringu (2022)** -- arXiv:2209.10334
- **Method**: Classify trades by co-occurrence proximity into 5 types. Conditional Order Imbalance (COI) per type.
- **Results**: COI strategies achieve "conspicuous returns and Sharpe ratios" on 457 stocks over 4 years (daily frequency).
- **Key insight**: Isolated trades -> positive future return association. Co-occurring trades -> negative (reversal). Trade co-occurrence classifiable from L1 trade data alone.
- **Applicability**: **Feasible with our data** -- only needs trade timestamps and prices, not MBO.

### 2.2 Order Book Filtration & Flicker Removal

**[OB Filtration] arXiv:2507.22712** (July 2025)
- **Method**: Three filtration schemes (order lifetime, update count, inter-update delay) applied to raw LOB events before computing OBI. Diagnostic via Hawkes excitation norms.
- **Results**: Filtering aggregate order flow -> modest improvement. Filtering **parent orders of executed trades** -> systematically stronger directional signal.
- **Key insight**: Flickering liquidity (rapid submit-cancel) contaminates OBI. Filtering improves signal-to-noise.
- **Applicability**: Partially feasible. We can compute snapshot-diff based indicators and filter by lifetime of price level persistence.

### 2.3 Cross-Asset Order Flow Imbalance

**[Cross-Impact OFI] Cont & Kokot (2021)** -- arXiv:2112.13213
- **Method**: PCA-integrated multi-level OFI; cross-asset OFI via LASSO for 100 S&P 500 stocks.
- **Results**: Multi-level integrated OFI explains price impact better than L1 OFI. Lagged cross-asset OFI improves return forecasting at short horizons (decays rapidly).
- **Key insight**: Cross-impact is sparse (few assets matter) and short-lived.
- **Applicability**: **Directly feasible** -- we have L5 snapshots for both 2330 (stock) and TXFD6 (futures). Can compute multi-level OFI on both and test cross-impact.

**[Hawkes OFI Forecasting] Muni Toke & Yoshida (2024)** -- arXiv:2408.03594
- **Method**: Hawkes process to model lagged bid-ask OFI dependence, forecast near-term OFI distribution.
- **Results**: Forecasted OFI indicates adverse selection in advance. Useful for regulators and market makers.
- **Applicability**: Requires trade-by-trade event data. Partially feasible with our L5 diff reconstruction.

### 2.4 Deep Learning on LOB

**[Deep LOB Forecasting] Kolm et al. (2024)** -- arXiv:2403.09267
- **Method**: Comprehensive benchmark of DL models (DeepLOB, LSTM, Transformer, etc.) on NASDAQ L5 data.
- **Results**: DL models achieve high classification accuracy for mid-price direction. **But**: "high forecasting power does not necessarily correspond to actionable trading signals" after costs.
- **Key insight**: Statistical prediction != tradeable alpha. Microstructural characteristics (tick size, spread, volatility) determine whether prediction -> profit.
- **Relevance**: Cautionary -- DL prediction accuracy alone insufficient. Cost model essential.

**[T-KAN] (2026)** -- arXiv:2601.02310
- **Method**: Temporal Kolmogorov-Arnold Networks replace LSTM's fixed weights with learnable B-spline activations. Applied to L5 LOB forecasting.
- **Results**: 19.1% F1 improvement over DeepLOB at k=100 horizon. **132% return vs -83% DeepLOB under 1.0 bps costs.**
- **Key insight**: Alpha decay is the critical challenge. T-KAN's interpretable splines show "dead zones" where signal is noise.
- **Applicability**: Architecture applicable to our L5 data. But: 1.0 bps cost assumption is generous for TXFD6 (2.0 bps RT).

**[TLOB] (2025)** -- arXiv:2502.15757
- **Method**: Transformer with dual attention (spatial across price levels + temporal across time) for LOB.
- **Results**: Best on recent Bitcoin LOB data. Captures cross-level dependencies.
- **Applicability**: Architecture idea; spatial attention across L5 levels is relevant.

**[HLOB] (2024)** -- arXiv:2405.18938
- **Method**: Information Filtering Networks to extract dependency structures among volume levels before feeding to DL.
- **Results**: Improved mid-price forecasting by leveraging inter-level volume correlations.
- **Applicability**: Feature engineering idea for L5 data.

### 2.5 Toxic Flow Detection

**[Detecting Toxic Flow / PULSE] Cont, Cucuringu et al. (2023)** -- arXiv:2312.05827
- **Method**: Online Bayesian method (PULSE) to predict trade toxicity in <1ms. Uses per-trade features from broker flow.
- **Results**: High, stable AUC for toxicity prediction. Real-time executable.
- **Limitation**: Requires broker client-level data (which trades are from informed vs uninformed). **Not feasible without broker data.**

**[Simple Strategy for Toxic Flow] Cartea & Sanchez-Betancourt (2025)** -- arXiv:2503.18005
- **Method**: Infinite-horizon stochastic control for broker dealing with informed/uninformed clients.
- **Results**: Closed-form optimal strategy balancing flow maximization vs adverse selection minimization.
- **Relevance**: Theoretical framework. Not directly applicable as we're a taker, not a broker.

### 2.6 Queue-Reactive Models

**[Deep Queue-Reactive / MDQR] (2025)** -- arXiv:2501.08822
- **Method**: Extends queue-reactive model with DL: relaxes queue independence, enriches state space, models order size distribution.
- **Results**: Better simulation of order book dynamics, particularly at tick level.
- **Limitation**: Needs true event-level data for calibration. Primarily for simulation, not direct alpha.

**[RL in Queue-Reactive] (2025)** -- arXiv:2511.15262
- **Method**: Reinforcement learning for optimal execution within queue-reactive LOB simulator.
- **Results**: Captures both direct and indirect market impact.
- **Relevance**: Execution optimization, not alpha generation.

### 2.7 Hawkes Process & Point Process Models

**[Neural Hawkes LOB] (2025)** -- arXiv:2502.17417
- **Method**: Neural Hawkes Process for midprice modeling and LOB simulation.
- **Results**: Better LOB event simulation than traditional Hawkes or other DL methods.
- **Relevance**: Modeling framework. Could be adapted for OFI forecasting.

**[Hawkes Crypto LOB] (2023)** -- arXiv:2312.16190
- **Method**: Hawkes + COE model for next price change timing prediction from LOB data.
- **Results**: Improved return sign prediction using point process timing information.
- **Applicability**: Timing prediction feasible with our L5 snapshot timestamps.

---

## 3. Candidate Alpha Directions

### Candidate A: Cross-Asset L5 OFI with PCA Integration (2330 -> TXFD6)

**Paper basis**: Cont & Kokot (2021, arXiv:2112.13213), Muni Toke & Yoshida (2024, arXiv:2408.03594), Kolm et al. (2024, arXiv:2403.09267)

**Core signal**: Compute multi-level OFI (L1-L5) on both TSMC 2330 stock and TXFD6 futures. Use PCA to integrate across levels (per Cont & Kokot). Then: lagged 2330-OFI -> TXFD6 return prediction. The hypothesis is that informed flow in the underlying (2330) manifests in the order book before the futures price adjusts.

**Why it differs from R15/R18**:
- R15 tested LOB shape features (depth asymmetry, KE) on TXFD6 alone -- found L3-L5 adds noise. But PCA-integrated multi-level OFI is a fundamentally different transformation: it finds the linear combination of L1-L5 OFI that maximally explains price impact, rather than using raw depth metrics.
- R18 tested MLOFI (multi-level OFI with depth weighting) but only as a microprice adjustment within a single asset. Failed due to trend contamination. This candidate uses **cross-asset** OFI (2330 -> TXFD6), which is genuinely new information (different asset's order book).
- R17 tested TSMC lead-lag at L1 only (IC=0.061, p=0.066). Adding L5 OFI integration could push over the threshold since deeper levels capture informed flow that doesn't appear at L1.

**Data requirements**: `research/data/l5/TXFD6_l5.npy` + `research/data/l5/2330_l5.npy` (overlapping dates). Also L5 v2 data for out-of-sample.

**Estimated IC and horizon**:
- Cont & Kokot report R-squared improvement of ~5-15% from cross-impact at 1-5s horizons on US equities
- R17 found TSMC->TXFD6 IC=0.061 at L1; with L5 OFI integration expect IC=0.07-0.10
- Target horizon: 30-120s (where cost breakeven IC ~ 0.03-0.04)
- **Detrended IC**: Cross-asset OFI is inherently detrended (it's the OTHER asset's order flow, not the asset's own price trend). Less susceptible to trend contamination than single-asset signals.

**Cost model**:
- TXFD6 RT cost: 2.0 bps (4 pts)
- At 60s horizon, IC breakeven = 0.030 (from R17 analysis)
- Need detrended IC > 0.05 to be viable after slippage and model degradation
- Cross-asset signal may enable better timing for CBS entries (combining with existing strategy)

**Key risks**:
1. 2330 and TXFD6 L5 data may not have sufficient temporal overlap (different exchanges, different tick rates)
2. TXFD6 is a thin book -- L3-L5 may be too sparse for meaningful OFI
3. Latency: 2330 quote -> our system -> compute -> trade TXFD6 may exceed signal half-life
4. R17 found TSMC lead-lag was marginal standalone -- PCA may not rescue it

---

### Candidate B: LOB Shape Regime Detection via Snapshot Clustering

**Paper basis**: ClusterLOB (Zhang et al., 2025, arXiv:2504.20349), HLOB (2024, arXiv:2405.18938), Non-parametric Regime Detection (2023, arXiv:2306.15835)

**Core signal**: Cluster L5 snapshot shapes (bid/ask volume profiles across 5 levels) into discrete regimes using K-means or GMM. Compute regime-conditional OFI signals -- the same OFI value has different predictive content depending on the current LOB shape regime. Signal = OFI x regime_indicator.

**Why it differs from R15/R18**:
- R15 used continuous LOB features (depth asymmetry, KE) as direct predictors. Failed because L3-L5 features were noisy and collinear with L1.
- This candidate uses LOB shape as a **regime filter**, not a direct predictor. The alpha comes from OFI (which works at L1), but the LOB shape tells you **when** OFI is more/less predictive.
- R18 MLOFI weighted OFI by depth -- a continuous transform. Clustering is fundamentally different: it identifies discrete states where market dynamics change qualitatively.
- Analogy: Like CBS's time-of-day gating but for LOB microstructure state. R14's CBS works because opening = momentum, rest = mean-reversion. Similarly, thin-book vs thick-book may have different OFI -> return relationships.

**Data requirements**: `research/data/l5/TXFD6_l5.npy` (2.17M snapshots, 10 days). L5 v2 for OOS.

**Estimated IC and horizon**:
- ClusterLOB reports SR improvement from ~1.0 (no cluster) to ~1.4-3.3 (with cluster) on NASDAQ stocks
- Our signal is the OFI conditional on regime, not the regime itself
- Expected detrended IC: 0.03-0.06 at 30-60s horizon (regime gating amplifies existing OFI signal)
- Conservative: if base OFI IC = 0.02, regime gating might lift to 0.04-0.05

**Cost model**:
- Same TXFD6 cost: 2.0 bps (4 pts)
- Regime gating reduces trade frequency (only trade in favorable regimes) -> higher per-trade edge
- If thick-book regime (favorable for OFI) occurs 30-40% of the time, and IC doubles in that regime, net edge can overcome costs
- Key: regime gating must be implemented as a FILTER on existing strategies, not standalone

**Key risks**:
1. TXFD6 L5 book is thin (volumes 1-10 per level). Clustering thin books may produce unstable regimes
2. 10 days of L5 data is marginal for regime calibration (need regime transitions to be frequent enough)
3. Regime transitions may be too slow for intraday trading (if book shape is static for hours, no alpha)
4. Overfitting risk: many possible cluster configurations, only 10 days of data
5. R15 finding that L3-L5 adds noise may repeat -- regime detection using noisy features produces noisy regimes

---

### Candidate C: Trade Co-occurrence Conditional OFI (Adapted from COI)

**Paper basis**: Lu, Reinert & Cucuringu (2022, arXiv:2209.10334), Order Book Filtration (arXiv:2507.22712), Hawkes OFI (arXiv:2408.03594)

**Core signal**: Classify TXFD6 trades by temporal co-occurrence patterns: isolated trades (no nearby trades) vs clustered bursts. Compute Conditional Order Imbalance (COI) separately for isolated and clustered trades. Isolated-trade OFI predicts continuation; clustered-trade OFI predicts reversal (adverse selection signature). Use the differential signal.

**Why it differs from R15/R18**:
- R15/R18 used aggregate LOB features (depth, OFI) without distinguishing trade context. This decomposes the SAME OFI signal by the microstructural context of the trades that generated it.
- Not depth-dependent: works entirely with L1 trade data (trade timestamps, prices, volumes). L3-L5 noise is irrelevant.
- The filtration paper (2507.22712) confirms: filtering by trade parent-order characteristics -> "systematically stronger directional signal". Our adaptation uses trade co-occurrence as a proxy for informed vs noise flow.
- R16 found "signal-horizon mismatch" (signals 5-15s, costs need 60s). COI's key innovation is that **isolated trades** have longer-lasting signal (information persists when not immediately arbitraged away by co-occurring flow).

**Data requirements**: L1 trade data for TXFD6 (extensive: 40+ days available). Optionally L5 snapshots for context.

**Estimated IC and horizon**:
- Lu et al. report "conspicuous returns and Sharpe ratios" on daily equity data (different setting)
- Our adaptation at intraday: expect detrended IC = 0.02-0.05 at 30-120s
- Isolated-trade signal should have longer half-life than aggregate OFI (the whole point of decomposition)
- The differential (isolated_COI - clustered_COI) should be relatively orthogonal to trend

**Detrended IC reasoning**: Trade co-occurrence classification is based on **timing structure**, not price direction. An isolated buy in a trending market has the same classification as an isolated buy in a mean-reverting market. The COI signal is structurally less susceptible to trend contamination than price-based or depth-based features.

**Cost model**:
- TXFD6 RT cost: 2.0 bps (4 pts)
- At 60s horizon, need IC > 0.030
- Signal is a trade filter -> can be used as CBS enhancement (gate trades by flow regime)
- Best use: reduce false positives in existing strategies rather than standalone alpha
- If isolated-trade COI achieves IC=0.04 at 60s, and we trade only when it agrees with CBS, the combined strategy may clear the cost hurdle

**Key risks**:
1. TXFD6 median tick interval = 125ms. "Isolated" vs "clustered" classification depends on threshold -- may be unstable
2. TAIFEX doesn't provide trade-side classification (no Lee-Ready or similar). Must infer from price movement
3. Daily data -> intraday translation may lose the effect (Lu et al. used daily buckets)
4. 2.0 bps cost is high for a flow-type signal -- may only be viable as a filter, not standalone
5. Limited data: need to define co-occurrence windows, which is another parameter to overfit

---

## 4. Comparison Matrix

| Criterion | A: Cross-Asset L5 OFI | B: LOB Shape Regime | C: Trade Co-occurrence COI |
|-----------|----------------------|--------------------|-----------------------------|
| **Paper support** | Strong (Cont & Kokot 2021) | Moderate (ClusterLOB 2025) | Moderate (Lu et al. 2022) |
| **Data feasibility** | Partial (need temporal overlap) | Good (L5 available) | Good (L1 extensive) |
| **Prior negative dodge** | Yes (new: cross-asset) | Partial (still uses LOB depth) | Yes (new: trade decomposition) |
| **Detrended IC safety** | High (cross-asset = natural detrend) | Medium (regime is price-independent but features aren't) | High (timing-based classification) |
| **Estimated IC range** | 0.05-0.10 | 0.03-0.06 | 0.02-0.05 |
| **Standalone viability** | Possible | Unlikely (filter only) | Unlikely (filter only) |
| **Implementation complexity** | High (cross-asset alignment) | Medium (clustering + gating) | Low-Medium (threshold tuning) |
| **Overfit risk** | Medium (PCA on 10 days) | High (cluster + 10 days L5) | Medium (threshold + 40 days) |
| **Cost model feasibility** | Possible at 60s+ | Only as filter | Only as filter |

---

## 5. Recommendations

### Primary: Candidate A (Cross-Asset L5 OFI)
**Rationale**: Genuinely new information source (2330 LOB) that was only tested at L1 in R17. PCA-integrated multi-level OFI is a well-established methodology with strong paper support. Natural detrending property. Highest standalone IC potential.

### Secondary: Candidate C (Trade Co-occurrence COI)
**Rationale**: Uses abundant L1 data (40+ days), lowest implementation complexity, and addresses the R16 signal-horizon mismatch by identifying trades with longer signal persistence. Best suited as CBS strategy enhancement.

### Deprioritize: Candidate B (LOB Shape Regime)
**Rationale**: Highest overfit risk (10 days L5 data + clustering parameters), and R15 already showed L3-L5 features are noisy on TXFD6's thin book. Could be revisited when more L5 data accumulates.

### Kill conditions (apply to all candidates):
1. Detrended IC < 0.02 on latest 3 days -> KILL immediately
2. IC sign reversal across calibration windows -> KILL
3. Signal half-life < 10s -> KILL (can't execute at 36ms RTT)
4. Trade frequency in favorable regime < 5/day -> KILL (insufficient for statistical validation)

---

## 6. Papers Referenced

| # | Paper | ArXiv ID | Year | Key Contribution |
|---|-------|----------|------|------------------|
| 1 | Cross-Impact of OFI in Equity Markets | 2112.13213 | 2021 | PCA multi-level OFI, cross-asset impact |
| 2 | ClusterLOB | 2504.20349 | 2025 | MBO event clustering -> OFI decomposition |
| 3 | Trade Co-occurrence & COI | 2209.10334 | 2022 | Trade classification by co-occurrence |
| 4 | Order Book Filtration | 2507.22712 | 2025 | Flicker removal, parent-order filtering |
| 5 | Deep LOB Forecasting | 2403.09267 | 2024 | DL benchmark, prediction != profit warning |
| 6 | T-KAN LOB Forecasting | 2601.02310 | 2026 | KAN for LOB, alpha decay analysis |
| 7 | TLOB Dual Attention | 2502.15757 | 2025 | Spatial+temporal attention for LOB |
| 8 | HLOB Information Filtering | 2405.18938 | 2024 | Inter-level volume dependency structures |
| 9 | Hawkes OFI Forecasting | 2408.03594 | 2024 | Hawkes process for OFI prediction |
| 10 | Detecting Toxic Flow (PULSE) | 2312.05827 | 2023 | Online Bayesian toxicity prediction |
| 11 | Simple Strategy for Toxic Flow | 2503.18005 | 2025 | Optimal broker dealing with informed flow |
| 12 | Hawkes Crypto LOB | 2312.16190 | 2023 | Point process timing for return prediction |
| 13 | Neural Hawkes LOB | 2502.17417 | 2025 | Neural Hawkes for LOB simulation |
| 14 | Deep Queue-Reactive (MDQR) | 2501.08822 | 2025 | DL + queue-reactive LOB model |
| 15 | Non-parametric Regime Detection | 2306.15835 | 2023 | Online regime clustering |
| 16 | Limit Order Book Simulations Review | 2402.17359 | 2024 | LOB simulation methods survey |

---

## 7. Data Gap Assessment

For more robust research, the following data improvements would help:
1. **More L5 days**: 10 days is marginal for cross-validation. 20+ days preferred.
2. **True MBO data**: Would unlock ClusterLOB-style approaches. Requires TAIFEX full-depth feed.
3. **Cross-asset temporal alignment**: Verify 2330 and TXFD6 L5 timestamps overlap cleanly.
4. **TXFE6 (E-mini) L5**: Available in v2 -- can test cross-asset with near-expiry futures.

# Round 18 Stage 1: LOB-Driven Medium-Frequency Strategy Survey

**Date**: 2026-03-26
**Scope**: arXiv papers on order-book-driven directional strategies with 3-30 minute holding periods
**Target**: TXFD6 / TMFD6 (Taiwan futures)
**Constraints**: ~36ms RTT, ~4 pts RT cost (TMFD6), ~39 NTD RT (TXFD6), no maker rebates, L1 micro exhausted

---

## Search Methodology

Queried arXiv (q-fin.TR, q-fin.ST, q-fin.MF, cs.LG) with 10+ variations. Reviewed ~120 results,
downloaded and read 3 full papers. 3 candidates survived filtering against R12-R17 findings.

## Prior Round Exclusion List

| Signal Class | Round | Why Dead |
|---|---|---|
| L1 OFI / depth imbalance | R9-R11, R16 | IC decays <15s, signal-horizon mismatch |
| Push-response | R16 | Regime-dependent, not structural |
| Entropy / toxicity flow | R16 | Spread >> signal edge |
| LOB kinetic energy / gravity | R15 | IC < 0.025, collinear with depth_imbalance |
| MLOFI gradient | R11 | IC=-0.105 but fees > returns (Gate C FAIL) |
| Pure MM / spread capture | R13, OpMM | Spread < RT cost structurally |
| TSMC lead-lag | R17 | IC=0.061, p=0.066, fails kill gates |
| OIDS (options volume) | R17 | TXO 99.7% quotes, no trade data |

**Key constraint**: Signals at 5-15s horizons are dead. Need predictive power at 3-30 min
(IC breakeven: 0.043 at 30min, 0.030 at 60min).

---

## Candidate A: Generalized Stationarized OFI (log-GOFI)

### Paper
- **arXiv**: 2112.02947v1
- **Title**: "The Price Impact of Generalized Order Flow Imbalance"
- **Authors**: Su, Sun, Li, Yuan (Xi'an Jiaotong University, 2021)

### Core Signal Logic
Standard OFI (Cont 2014) assumes BBO moves by one tick between snapshots. **GOFI** relaxes this:
it tracks order quantity changes across ALL price levels the BBO traversed, not just the single
best level. **log-GOFI** applies log stationarization to reduce heteroscedasticity from heterogeneous
queue depths. This captures "total pressure that caused the last move" -- a flow-integral, not a
snapshot.

Out-of-sample R-squared for contemporaneous mid-price changes on CSI 500 stocks:

| Metric | 30s | 1min | 5min |
|---|---|---|---|
| Standard OFI | 32.89% | 38.13% | 42.57% |
| **log-GOFI** | **83.57%** | **85.37%** | **86.01%** |

R-squared INCREASES with horizon (83.6% to 86.0% from 30s to 5min). The signal gets more
explanatory at longer windows -- exactly what we need.

### Expected Holding Period
5-15 minutes. Compute log-GOFI over rolling 5-minute windows; use as directional signal.

### Data Requirements vs What We Have
- **Required**: Multi-level LOB snapshots (price + volume per level)
- **We have**: 5-level LOB tick-by-tick for TXFD6/TMFD6 -- BETTER than the paper's 3s snapshots
- **Implementation**: Track which price levels BBO traversed between updates; aggregate depth
  changes across all traversed levels; apply log(q) stationarization

### Key Risks
1. **Contemporaneous vs predictive**: Paper measures R-squared in same window, not t to t+1
   prediction. Prior rounds showed high contemporaneous but weak predictive OFI at short horizons.
   The multi-level aggregation + stationarization may create persistence, but must be tested.
2. **CSI 500 stocks != TXFD6 futures**: Different microstructure, tick, spread, participant mix.
3. **Overlap with existing features**: We have `ofi_depth_norm_ppm` in FE v2. log-GOFI is
   structurally different (aggregates across traversed levels) but collinearity risk exists.

### Differentiation from Prior Rounds
- R9-R11: Standard OFI/MLOFI at L1, short horizons (seconds). log-GOFI aggregates across ALL
  traversed levels with log stationarization. Novel construction.
- Prior features: "current state of book." log-GOFI: "total pressure that caused the last move."

### Verdict: CONDITIONAL APPROVE
Key gate: predictive IC > 0.043 at 30-minute horizon.

---

## Candidate B: OFI Regime-Switching with Optimal Horizon (quasi-Sharpe Framework)

### Paper
- **arXiv**: 2505.17388v1
- **Title**: "Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures"
- **Authors**: Chen Hu, Kouxiao Zhang (Guolian Futures, 2025)

### Core Signal Logic
Models OFI price response as an **Ornstein-Uhlenbeck process** -- a shock that initiates rapidly
then mean-reverts. The **quasi-Sharpe ratio** (E[return]/std[return] as function of time after
OFI shock) identifies the optimal holding period for each OFI signal.

Three findings:
1. **Horizon-dependent heterogeneity**: OFI predictive power varies with forecast horizon. Short =
   noise-dominated, long = mean-reversion kills signal. Optimal intermediate horizon exists.
2. **Regime-dependent dynamics**: OFI memory switches between "high" and "low" efficiency regimes.
   LOW efficiency = trading opportunity (market has not priced in OFI information).
3. **Metric screening**: Robust metrics show only quantitative (not sign) variations across regimes.

OFI contemporaneous correlation with returns on CSI 300 Index Futures:

| Horizon | 0.5s | 5s | 30s | 1m | 5m | 10m | 30m | 1h |
|---|---|---|---|---|---|---|---|---|
| OFI corr | 0.20 | 0.46 | 0.52 | 0.52 | 0.54 | 0.52 | 0.52 | 0.54 |

OFI correlation >0.50 stable from 5s to 60min. Extraordinary persistence if it transfers.

### Expected Holding Period
5-30 minutes, optimized via quasi-Sharpe ratio peak (principled, not grid-searched).

### Data Requirements vs What We Have
- **Required**: LOB for OFI + OU parameter fitting + regime detection
- **We have**: OFI already in FeatureEngine. OU fitting and regime detection (autocorrelation
  decay in rolling windows) are straightforward additions.

### Key Risks
1. **CSI 300 != TXFD6/TMFD6**: CSI 300 is very liquid. TMFD6 lower liquidity, wider spreads.
   OU mean-reversion speed likely different.
2. **Our R16 finding**: OFI signals work 5-15s but die at 60s+ on TMFD6. This paper claims
   persistence to 60min on CSI 300. Discrepancy may be instrument-specific.
3. **Regime detection latency**: Lookback window trade-off (long = slow, short = noisy).
4. **Contemporaneous correlation != predictive alpha**.

### Differentiation from Prior Rounds
- R16: OFI as "use it or lose it." This paper: OFI as SHOCK with TIME PROFILE. Innovation is
  not "OFI predicts prices" but "OFI has optimal horizon that varies with regime."
- R12: VPIN regime overlay (failed). OFI-regime uses signal's own autocorrelation, not external.
- Potential CBS integration: regime gate for CBS (trade only in "low efficiency" regime).

### Verdict: CONDITIONAL APPROVE
Key gate: OFI correlation >0.10 at 5-minute horizon on TXFD6/TMFD6.

---

## Candidate C: ClusterLOB -- Participant-Decomposed OFI

### Paper
- **arXiv**: 2504.20349v3
- **Title**: "ClusterLOB: Enhancing Trading Strategies by Clustering Orders in Limit Order Books"
- **Authors**: Zhang, Cucuringu, Shestopaloff, Zohren (Oxford, 2025)

### Core Signal Logic
Classifies individual orders into 3 participant types via K-means++ on 6 features:
- **Directional**: Large, aggressive, low cancellation
- **Opportunistic**: Medium, moderate aggressiveness
- **Market-Making**: Symmetric, high cancellation, passive

Computes separate OFI per cluster in 30-minute buckets. **Directional OFI is far more predictive
than aggregate OFI** because MM flow dilutes signal. Tested on 1 year NASDAQ MBO data; cluster-
decomposed OFI in 30-min buckets shows higher Sharpe than undifferentiated OFI.

### Expected Holding Period
30 minutes (paper uses 30-min buckets). Well-suited to our cost structure.

### Data Requirements vs What We Have
- **Required**: Market-by-order (MBO) data with individual order IDs, types, lifetimes
- **We have**: LOB snapshots (aggregate state), NOT individual order messages
- **CRITICAL GAP**: Cannot directly replicate clustering. Workaround: classify LOB state changes
  as "aggressive" (one-sided, large, fast) vs "passive" (symmetric, gradual) and compute separate
  OFI streams. Significant methodological deviation.

### Key Risks
1. **Data gap is severe**: Without MBO data, building approximation, not replication.
2. **NASDAQ equities != Taiwan futures**: Different participant mix (retail-dominated).
3. **Paper does not report absolute PnL** after costs, only relative improvement.
4. **Clustering stability**: K-means may not be stationary across market conditions.

### Differentiation from Prior Rounds
- No prior round decomposed order flow by participant type. All OFI work used aggregate flow.
- Concept orthogonal to signal construction -- about signal SOURCE decomposition.
- Even approximate "aggressive vs passive" decomposition may add value.

### Verdict: CONDITIONAL APPROVE (secondary priority)
Data gap makes this highest risk. Pursue only after A and B are tested.

---

## Summary and Prioritization

| Rank | Candidate | Paper | Innovation | Hold | Data Fit | Key Gate |
|---|---|---|---|---|---|---|
| 1 | A: log-GOFI | 2112.02947 | Multi-level aggregated OFI + log stationarization | 5-15m | Good | Predictive IC > 0.043 at 30min |
| 2 | B: OFI-OU Regime | 2505.17388 | OU shock model + regime switch + quasi-Sharpe | 5-30m | Good | OFI corr >0.10 at 5min on TXFD6 |
| 3 | C: ClusterLOB | 2504.20349 | Participant-decomposed OFI | 30m | Poor | Heuristic approximation valid |

### Recommended Stage 2 Plan
1. Prototype log-GOFI on TXFD6: compute generalized construction, measure predictive IC at 5/10/30 min
2. Measure OFI persistence on TXFD6/TMFD6: autocorrelation + correlation with returns at 30s-30m
3. If A or B show promise: attempt ClusterLOB-inspired aggressive/passive OFI decomposition

### Excluded Papers and Why
- **Deep LOB Forecasting** (Briola 2024, 2403.09267): Prediction horizon is ticks not minutes;
  authors warn "high forecasting power does not necessarily correspond to actionable trading signals"
- **RL Execution Agents** (Nagy 2023, 2301.08688): Execution optimization, not signal generation
- **Cox-type Order Flow** (Toke 2018, 1805.06682): Next-event prediction, not minutes-horizon
- **Returns and OFI** (Takahashi 2025, 2508.06788): "Shocks dissipate within a second" on E-mini;
  confirms our finding that tick-level OFI decays fast

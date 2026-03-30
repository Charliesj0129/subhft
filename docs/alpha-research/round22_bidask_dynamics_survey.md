# Round 22: Bid/Ask Snapshot Dynamics Survey

**Date**: 2026-03-28
**Scope**: Extracting predictive signals from sequences of L1-L5 bid/ask snapshots
**Data**: TAIFEX futures (TMFD6/TXFD6), nanosecond tick+bidask, L1-L5 depth snapshots
**Constraint**: No individual order IDs (MBO data) -- must infer events from snapshot diffs

---

## Executive Summary

This survey covers 25+ papers on extracting predictive signals from limit order book (LOB) snapshot sequences. The key finding is that **the sequence of LOB states -- not individual snapshots -- contains the majority of short-horizon predictive information**, and that **feature engineering on snapshot differences matters more than model architecture**.

### Top Candidates for Our Setup (Ranked by Feasibility)

| Rank | Direction | Paper Basis | Feasibility | Expected Horizon |
|------|-----------|-------------|-------------|------------------|
| 1 | **Snapshot-Diff OFI (Multi-Level)** | Xu et al. 2019 | HIGH -- direct from L1-L5 diffs | 1s-60s |
| 2 | **Cancellation Rate Asymmetry** | Anantha et al. 2025 | HIGH -- inferred from depth decreases | 5s-30s |
| 3 | **Queue Imbalance Dynamics** | Gould & Bonart 2016 | HIGH -- L1 volumes only | 1-tick ahead |
| 4 | **Depth Change Velocity** | Briola et al. 2024 | MEDIUM -- rolling window on diffs | 10s-60s |
| 5 | **LOB State Markov Transitions** | Cont & de Larrard 2013 | MEDIUM -- state encoding needed | 1s-30s |
| 6 | **Order Flow Image Representation** | Lensky & Hao 2023 | LOW -- CNN training required | 5s-300s |

---

## Part A: LOB Snapshot Sequence Prediction (Deep Learning)

### P01: DeepLOB -- Deep Convolutional Neural Networks for Limit Order Books

- **ID**: arXiv:1808.03668
- **Authors**: Zihao Zhang, Stefan Zohren, Stephen Roberts (Oxford)
- **Year**: 2018
- **Methodology**: CNN + Inception Module + LSTM. Takes raw L1-L10 LOB snapshots (price+volume at each level) as input. CNN extracts spatial features across levels, LSTM captures temporal dependencies across snapshot sequence.
- **Key Findings**:
  - Outperforms all prior models on FI-2010 benchmark
  - Transfers well to unseen instruments (universal feature extraction)
  - First convolutional layer aggregates price and volume per level; subsequent layers combine across adjacent levels
  - Volume imbalance is the dominant learned feature
- **Horizon**: 1-50 events ahead (~seconds to minutes)
- **Data Requirements**: L1-L10 price+volume snapshots at event frequency
- **Applicability**: **HIGH**. Our L1-L5 data is exactly the input format. However, the model is a black box and requires GPU training. The learned features (primarily imbalance-like) can be replicated with hand-crafted features at lower complexity.

### P02: Deep Limit Order Book Forecasting -- A Microstructural Guide

- **ID**: arXiv:2403.09267
- **Authors**: Antonio Briola, Silvia Bartolucci, Tomaso Aste (UCL)
- **Year**: 2024
- **Methodology**: Comprehensive benchmark of deep learning models (DeepLOB, LSTM, Transformer variants) on NASDAQ LOB data. Releases LOBFrame open-source framework.
- **Key Findings**:
  - **Stocks' microstructural characteristics influence model efficacy** -- tick size relative to spread is critical
  - Small-tick stocks (spread >> tick): higher predictability but lower actionability
  - Large-tick stocks (spread ~ tick): lower predictability but more actionable
  - Traditional ML metrics (accuracy, F1) fail to assess forecast quality in LOB context
  - **Spatial information across depth levels degrades over longer prediction horizons** -- L1 dominates at long horizons
  - Proposed operational framework evaluating probability of complete transaction prediction
- **Horizon**: 1-50 events, multiple time horizons
- **Data Requirements**: L1-L10 snapshots at event frequency
- **Applicability**: **CRITICAL REFERENCE**. Their finding about spatial information degradation directly explains our R15 result (L3-L5 add noise). TMFD6 with median spread = 3 pts and tick = 1 pt is a "medium-tick" instrument -- moderate predictability.

### P03: TLOB -- Transformer with Dual Attention for LOB Price Trend Prediction

- **ID**: arXiv:2502.15757
- **Authors**: Leonardo Berti, Gjergji Kasneci
- **Year**: 2025
- **Methodology**: Dual attention mechanism -- one spatial (across LOB levels) and one temporal (across time steps). New labeling method removing horizon bias.
- **Key Findings**:
  - Outperforms DeepLOB on FI-2010, NASDAQ, and Bitcoin datasets
  - Particularly effective for longer-horizon predictions and volatile conditions
  - Labeling method matters: spread-aware labels (trends defined relative to average spread) dramatically reduce performance, highlighting gap between prediction and profitability
  - **Predictability has declined over time** (-6.68 F1 score)
- **Horizon**: 10-200 events
- **Data Requirements**: L1-L10 snapshots
- **Applicability**: **MEDIUM**. The spread-aware labeling finding is directly relevant -- our TMFD6 spread = 3 pts with cost = 3.92 pts makes profitable classification extremely hard. Confirms R16 structural finding.

### P04: HLOB -- Information Persistence and Structure in Limit Order Books

- **ID**: arXiv:2405.18938
- **Authors**: Antonio Briola, Silvia Bartolucci, Tomaso Aste
- **Year**: 2024
- **Methodology**: Uses Triangulated Maximally Filtered Graph (TMFG) from information filtering to uncover non-trivial dependency structures among LOB volume levels. Homological CNN architecture.
- **Key Findings**:
  - **Spatial distribution of information in LOB degrades over prediction horizons** with velocity dependent on microstructural properties
  - Non-trivial inter-level dependencies exist beyond simple imbalance
  - TMFG reveals that certain level combinations are more informative than naive level ordering
  - Performance advantage over DeepLOB mainly at short horizons
- **Horizon**: 1-50 events
- **Data Requirements**: L1-L10 snapshots
- **Applicability**: **MEDIUM**. The inter-level dependency finding is interesting but TMFG construction adds latency. Key takeaway: information in deeper levels decays faster than L1.

### P05: Exploring Microstructural Dynamics in Cryptocurrency LOBs -- Better Inputs Matter More Than Stacking Another Hidden Layer

- **ID**: arXiv:2506.05764
- **Authors**: Haochuan Wang (U. Chicago)
- **Year**: 2025
- **Methodology**: Benchmarks logistic regression, XGBoost, DeepLOB, Conv1D+LSTM on BTC/USDT LOB snapshots at 100ms to multi-second intervals.
- **Key Findings**:
  - **Feature engineering and preprocessing dominate model architecture in importance**
  - Simple models with good features match or beat deep models with raw inputs
  - Flickering liquidity (rapid add/cancel) is a major noise source
  - Multi-level views are even more susceptible to flickering at each price level
  - Rolling window volatility, OFI across levels, and PCA summaries of LOB snapshots are top features
- **Horizon**: 100ms to seconds
- **Data Requirements**: L1-L5+ snapshots at 100ms+ frequency
- **Applicability**: **HIGH**. Directly validates hand-crafted feature approach over black-box DL. Our L1-L5 snapshots at tick frequency are suitable. Key implication: invest in feature engineering, not model complexity.

### P06: Representation Learning of LOB -- Comprehensive Study and Benchmarking

- **ID**: arXiv:2505.02139
- **Authors**: Muyao Zhong, Yushi Lin, Peng Yang
- **Year**: 2025
- **Methodology**: Introduces LOBench benchmark with China A-share data. Compares task-specific end-to-end models vs. representation learning approaches.
- **Key Findings**:
  - LOB data has strong autocorrelation, cross-feature constraints, and feature scale disparity
  - Pre-trained LOB representations transfer across downstream tasks
  - Task-specific models overfit to narrow objectives
- **Horizon**: Multiple
- **Data Requirements**: Multi-level LOB snapshots
- **Applicability**: **LOW** for our use case (China A-share market structure differs). Useful reference for feature representation principles.

### P07: An Efficient Deep Learning Model for Stock Price Movement Based on LOB

- **ID**: arXiv:2505.22678
- **Year**: 2025
- **Methodology**: Siamese architecture exploiting bid-ask symmetry. Processes bid and ask sides through shared weights, then combines.
- **Key Findings**:
  - Bid-ask difference features show greater stability than raw values
  - Exploiting structural symmetry reduces model parameters and improves generalization
- **Horizon**: Short-term price movement classification
- **Applicability**: **MEDIUM**. The bid-ask difference insight is directly applicable to hand-crafted features. We should compute diff features (ask_vol_i - bid_vol_i) rather than using raw volumes.

### P08: Attention-Based Reading, Highlighting, and Forecasting of the LOB

- **ID**: arXiv:2409.02277
- **Authors**: Jiwon Jung, Kiseop Lee
- **Year**: 2024
- **Methodology**: Compound multivariate embedding for spatiotemporal features. Sequence-to-sequence model forecasting entire multi-level LOB (not just mid-price).
- **Key Findings**:
  - Forecasting the entire LOB (all levels, prices and volumes) is feasible
  - Compound embedding captures complex inter-level relationships
  - Preserves ordinal structure of LOB (price ordering constraint)
- **Horizon**: Multi-step LOB state forecast
- **Applicability**: **LOW** for signal extraction, but interesting for simulation/what-if analysis.

---

## Part B: Order Flow Imbalance and Multi-Level Features

### P09: Multi-Level Order-Flow Imbalance in a Limit Order Book (MLOFI)

- **ID**: arXiv:1907.06230
- **Authors**: Ke Xu, Martin D. Gould, Sam D. Howison (Oxford)
- **Year**: 2019
- **Methodology**: Defines MLOFI as a vector quantity measuring net order flow at each price level. Linear regression of MLOFI against contemporaneous mid-price change.
- **Key Findings**:
  - **Out-of-sample R-squared improves with each additional price level** included in MLOFI
  - L1 contributes most, but L2-L5 add statistically significant incremental information
  - Order-flow activity deep in the LOB influences price formation
  - Linear model suffices for capturing the relationship
- **Horizon**: Contemporaneous (same-event) price change
- **Data Requirements**: L1-L5+ snapshots to compute snapshot diffs
- **Applicability**: **CRITICAL -- HIGHEST PRIORITY**. This is exactly our setup. MLOFI is computed from snapshot differences: delta_volume at each level between consecutive snapshots. We already have `ofi_depth_norm_ppm` in FeatureEngine v2 (index 16). The paper validates that multi-level OFI from snapshot diffs is the single most important feature. **Action**: Verify our implementation decomposes OFI per level, not aggregated.

### P10: The Price Impact of Generalized Order Flow Imbalance

- **ID**: arXiv:2112.02947
- **Authors**: Yuhan Su, Zeyu Sun, Jiarong Li, Xianghui Yuan
- **Year**: 2021
- **Methodology**: Extends OFI to handle non-minimum quotation units. Proposes Generalized OFI (GOFI) and log-transformed Stationarized GOFI (log-GOFI).
- **Key Findings**:
  - log-GOFI substantially improves out-of-sample R-squared vs. traditional OFI at 30s, 1min, 5min horizons
  - Stationarization (log transform) is critical for time-varying market conditions
  - Tested on CSI 500 high-frequency order book snapshots
- **Horizon**: 30s, 1min, 5min
- **Data Requirements**: Multi-level LOB snapshots
- **Applicability**: **HIGH**. The stationarization technique (log-OFI) is directly applicable to our OFI features. We should consider log-transforming our OFI signals to improve stationarity. Works at our target 30s+ horizon.

### P11: Forecasting High Frequency Order Flow Imbalance using Hawkes Processes

- **ID**: arXiv:2408.03594
- **Authors**: Aditya Nittur Anantha, Shashi Jain
- **Year**: 2024
- **Methodology**: Models bid and ask order flow as mutually exciting Hawkes processes. Forecasts near-term OFI distribution.
- **Key Findings**:
  - Hawkes process with Sum of Exponentials kernel gives best OFI forecast
  - Accounts for lagged cross-dependence between bid and ask flows
  - OFI asymmetry between bid/offer associated with price movement direction
- **Horizon**: Tick-level to seconds
- **Data Requirements**: Event-level order flow (can be inferred from snapshot diffs)
- **Applicability**: **MEDIUM**. Hawkes modeling adds complexity. However, the insight about bid-ask cross-excitation is valuable: a burst of cancellations on one side predicts activity on the other side. Could be approximated with simpler rolling-window cross-correlation.

### P12: Queue Imbalance as a One-Tick-Ahead Price Predictor

- **ID**: arXiv:1512.03492
- **Authors**: Martin D. Gould, Julius Bonart
- **Year**: 2016
- **Methodology**: Binary and probabilistic classifiers (logistic regression) between L1 queue imbalance and next mid-price movement.
- **Key Findings**:
  - **Strongly statistically significant relationship** between queue imbalance and price direction
  - Works across all 10 tested NASDAQ stocks
  - Simple logistic regression suffices
  - Imbalance = (V_bid - V_ask) / (V_bid + V_ask) at best bid/ask
- **Horizon**: One tick ahead
- **Data Requirements**: L1 bid/ask volumes only
- **Applicability**: **HIGH -- ALREADY IMPLEMENTED**. This is essentially our `depth_imbalance` feature. The paper validates it as the single strongest instantaneous predictor. However, one-tick-ahead prediction does not overcome the spread for trading. The value is as a building block for longer-horizon features (e.g., rolling imbalance changes).

---

## Part C: Order Event Inference from Snapshots

### P13: Order Book Filtration and Directional Signal Extraction at High Frequency

- **ID**: arXiv:2507.22712
- **Authors**: Aditya Nittur Anantha, Shashi Jain, Prithwish Maiti
- **Year**: 2025
- **Methodology**: Tests three real-time filtration schemes for LOB data: order lifetime filter, update count filter, and inter-update delay filter. Evaluates impact on OBI signal quality using contemporaneous correlations, discretized regime associations, and Hawkes excitation analysis.
- **Key Findings**:
  - **A large fraction of orders in electronic markets is transient** (fleeting orders) and their ephemeral character degrades OBI signal
  - Filtering the aggregate order flow produces only **modest** improvements
  - **Filtering parent orders of executed trades** (not the full flow) produces systematically stronger directional association
  - Fleeting orders at deeper levels are even noisier
- **Horizon**: Tick-level to seconds
- **Data Requirements**: Order-level data for full implementation; snapshot diffs for approximate version
- **Applicability**: **HIGH CONCEPTUAL VALUE**. We cannot filter individual orders (no MBO data), but we CAN approximate: snapshot diffs where depth changes persist for multiple snapshots are "real" orders, while single-snapshot depth blips are "fleeting." **Proposed feature**: `persistent_depth_change_ratio` = fraction of depth changes that persist for >= N snapshots. This filters flickering liquidity noise.

### P14: Investigating Limit Order Book Characteristics for Short Term Price Prediction

- **ID**: arXiv:1901.10534
- **Authors**: Faisal I. Qureshi
- **Year**: 2019
- **Methodology**: Random forest on hand-crafted LOB features (10 levels). Systematic feature importance analysis.
- **Key Findings**:
  - Top features: bid-ask spread, volume imbalance at L1, then L2 imbalance, then depth-weighted price
  - LOB shape features (slope, curvature) add marginal value
  - Feature importance drops sharply beyond L3
- **Horizon**: Short-term (seconds)
- **Data Requirements**: L1-L10 price+volume snapshots
- **Applicability**: **HIGH -- VALIDATES OUR APPROACH**. Confirms that L1-L3 carry most information, consistent with our R15 finding that L3-L5 add noise on TMFD6. Feature importance ranking guides our feature engineering priority.

### P15: Price Predictability in Limit Order Book with Deep Learning Model

- **ID**: arXiv:2409.14157
- **Authors**: Kyungsub Lee
- **Year**: 2024
- **Methodology**: Decomposes LOB prediction into volatility prediction and directional prediction. Tests volume imbalance as additional feature.
- **Key Findings**:
  - **Standard three-class prediction (up/down/stable) conflates volatility and direction**
  - When using price process alone, directional prediction is not substantial
  - **Volume imbalance significantly improves directional prediction** (L1 ~70% accuracy)
  - Inadequately defined target price process may incorporate past information, inflating metrics
- **Horizon**: Event-level
- **Data Requirements**: L1 volumes
- **Applicability**: **HIGH -- METHODOLOGY INSIGHT**. We must separate volatility prediction from directional prediction. Volume imbalance is the key directional feature. Also warns about label leakage in LOB prediction.

---

## Part D: LOB State Dynamics and Markov Models

### P16: Price Dynamics in a Markovian Limit Order Market

- **ID**: arXiv:1104.4596
- **Authors**: Rama Cont, Adrien de Larrard
- **Year**: 2013 (SIAM J. Financial Math.)
- **Methodology**: Models LOB as Markovian queueing system. Order arrivals, cancellations, and executions described by arrival rates. Derives analytical expressions for price change distributions.
- **Key Findings**:
  - **Probability of upward price move is a function of LOB state** (bid/ask queue sizes)
  - Duration between price changes depends on queue imbalance
  - Price volatility expressed in terms of order arrival and cancellation rates
  - Queue depletion dynamics predict price moves
- **Horizon**: Inter-event (tick-level)
- **Data Requirements**: Bid/ask queue sizes at L1 (can extend to deeper levels)
- **Applicability**: **HIGH -- THEORETICAL FOUNDATION**. This is the theoretical basis for queue imbalance predictors. Key insight for us: **the RATE of queue depletion matters, not just instantaneous imbalance**. Tracking how fast depth is depleting (from snapshot diffs) gives the cancellation/execution rate, which enters the price change probability formula.

### P17: Intraday Limit Order Price Change Transition Dynamics via Markov Analysis

- **ID**: arXiv:2601.04959
- **Authors**: Luwang, Mukhia, Sharma, Nurujjaman, Rai, Petroni
- **Year**: 2026
- **Methodology**: Discrete-time Markov chain on NASDAQ100 tick data. Categorizes consecutive price changes into 9 states. Estimates transition probability matrices for 6 intraday intervals.
- **Key Findings**:
  - **Systematic intraday patterns in price change inertia**: probability of consecutive zero changes peaks at open (defensive positioning), declines midday (price discovery), surges at close
  - High market cap stocks exhibit strongest inertia; low cap stocks show wider spreads and lower stability
  - Bid-side has 3 temporal phases (Opening, Midday, Closing); ask-side has 4 phases (sellers reposition earlier)
  - Transition matrices are surprisingly stable within each phase
- **Horizon**: Intraday regime identification
- **Data Requirements**: Tick-level price changes (available from our data)
- **Applicability**: **MEDIUM-HIGH**. The intraday regime finding aligns with our CBS strategy's time-of-day gating. **Proposed feature**: `price_change_inertia` = rolling estimate of consecutive-same-direction probability. This is a state-based regime indicator that could gate CBS entry. The bid/ask asymmetry in temporal phases is novel and testable on TMFD6.

### P18: Microstructure Modes -- Disentangling Joint Dynamics of Prices and Order Flow

- **ID**: arXiv:2405.10654
- **Authors**: Salma Elomari-Kessab, Guillaume Maitrier, Julius Bonart, Jean-Philippe Bouchaud
- **Year**: 2024
- **Methodology**: PCA on coarse-grained order flow and returns from Eurostoxx order-by-order data (3 years). Vector Auto-Regressive (VAR) model on extracted modes.
- **Key Findings**:
  - Identified "microstructure modes" separating bid-ask symmetric and anti-symmetric patterns
  - **Symmetric modes** (e.g., simultaneous depth increase on both sides = spread tightening) have **high R-squared prediction scores**
  - **Anti-symmetric modes** (e.g., bid depth up, ask depth down) predict price direction
  - VAR parameters extremely stable over time (universal microstructure law)
  - VAR model becomes marginally unstable with more lags (long-memory of flows)
  - Does NOT reproduce square-root impact law
- **Horizon**: Minute-scale
- **Data Requirements**: Order-by-order data (Eurostoxx); approximable from snapshot diffs
- **Applicability**: **HIGH -- NOVEL DIRECTION**. The symmetric/anti-symmetric decomposition is directly computable from our L1-L5 snapshot diffs:
  - `symmetric_depth_change` = (delta_bid_vol + delta_ask_vol) -- liquidity provision/withdrawal
  - `antisymmetric_depth_change` = (delta_bid_vol - delta_ask_vol) -- directional pressure
  - The symmetric mode predicts spread dynamics (useful for entry timing); the anti-symmetric mode predicts price direction (useful for signal). **Action**: Compute both as features from snapshot diffs and measure IC at 30s horizon.

---

## Part E: Spoofing, Fleeting Orders, and Cancellation Signals

### P19: Learning the Spoofability of Limit Order Books

- **ID**: arXiv:2504.15908
- **Year**: 2025
- **Methodology**: Neural networks predicting mid-price movement distributions to detect spoofing. Novel order flow variables based on multi-scale Hawkes processes.
- **Key Findings**:
  - Spoofing creates detectable depth patterns: large orders placed and quickly cancelled
  - Multi-scale order flow captures spoof signatures better than single-scale
  - Cryptocurrency exchanges show higher spoofability than traditional markets
- **Applicability**: **LOW** for signal extraction, but validates that rapid depth changes carry information about intent.

### P20: Protecting Retail Investors from Order Book Spoofing using GRU

- **ID**: arXiv:2110.03687
- **Year**: 2021
- **Methodology**: GRU model detecting spoofing from LOB time series.
- **Key Findings**:
  - Spoofing manifests as asymmetric depth additions followed by rapid cancellations
  - Detectable from snapshot sequences even without order-level data
- **Applicability**: **LOW** directly, but the pattern (depth spike then reversal) is a feature we can compute.

---

## Part F: Explainable Features and Cross-Asset Universality

### P21: Explainable Patterns in Cryptocurrency Microstructure

- **ID**: arXiv:2602.00776
- **Authors**: Bartosz Bieganowski et al.
- **Year**: 2026
- **Methodology**: CatBoost with SHAP analysis on BTC, LTC, ETC, ENJ, ROSE LOB data. Direction-aware GMADL objective. Top-of-book taker and fixed-depth maker backtests.
- **Key Findings**:
  - **Same features and SHAP dependence shapes are stable across assets** spanning an order of magnitude in market cap
  - Top features: order flow imbalance, bid-ask spread, depth, trade arrival patterns
  - Taker strategies fail during flash crashes (adverse selection); maker strategies survive
  - **Feature universality suggests microstructure patterns are structural, not asset-specific**
- **Horizon**: Seconds
- **Data Requirements**: LOB snapshots + trades
- **Applicability**: **HIGH -- VALIDATES UNIVERSALITY**. If features are universal across crypto assets, they should also apply to TAIFEX futures. The key features (OFI, spread, depth, trade arrival) are all computable from our data. The maker vs. taker finding is relevant: our CBS is a taker strategy and is thus vulnerable to adverse selection during volatility spikes.

### P22: Using Deep Learning for Price Prediction by Exploiting Stationary LOB Features

- **ID**: arXiv:1810.09965
- **Authors**: Tsantekidis, Passalis, Tefas, Kanniainen, Gabbouj, Iosifidis
- **Year**: 2018
- **Methodology**: Proposes stationary features extracted from LOB: percentage difference of prices to current mid-price, normalized volumes.
- **Key Findings**:
  - **Stationarization of LOB features is critical** for deep learning performance
  - Price levels should be expressed as relative to mid-price (removes non-stationarity)
  - Volume normalization across levels improves learning
  - CNN+LSTM on stationary features outperforms raw LOB input
- **Horizon**: Event-level prediction
- **Data Requirements**: L1-L10 snapshots
- **Applicability**: **HIGH -- FEATURE ENGINEERING PRINCIPLE**. We should express all depth features as deviations from reference (e.g., volume at level i as fraction of total depth, prices as offset from mid). This stationarization is a prerequisite for any ML approach.

### P23: Order Flow Image Representation for Short-Term Volatility Prediction

- **ID**: arXiv:2304.02472
- **Authors**: Artem Lensky, Mingyu Hao
- **Year**: 2023
- **Methodology**: Transforms order flow data (trade sizes, directions, LOB state) into images. Maps to colour channels. Trains CNN/ResNet/ConvMixer.
- **Key Findings**:
  - Image representation of order flow captures spatiotemporal patterns CNN can exploit
  - Simple 3-layer CNN with this representation beats more complex architectures
  - Aggregated hand-crafted features supplementing the image improve further
  - BTC/USDT perpetual futures, RMSPE = 0.85 (vs. 1.4 naive)
- **Horizon**: Short-term volatility (~seconds)
- **Data Requirements**: Trade-level data + LOB snapshots
- **Applicability**: **LOW** for our use case (no trade classification data, and volatility prediction not our primary objective). However, the principle of spatial encoding of LOB state is interesting.

---

## Part G: Continuous Double Auction and LOB Theory

### P24: Statistical Theory of the Continuous Double Auction

- **ID**: arXiv:cond-mat/0210475
- **Authors**: Eric Smith, J. Doyne Farmer, Laszlo Gillemot, Supriya Krishnamurthy
- **Year**: 2002
- **Methodology**: Microscopic dynamical statistical model of the continuous double auction. Mean-field approximations.
- **Key Findings**:
  - Order size (granularity) is a more significant determinant of market behavior than tick size
  - Explains concave price impact function from microstructural parameters
  - Zero-intelligence models capture institutional structure effects
  - Cancellation rates and order sizes are the key parameters governing LOB dynamics
- **Horizon**: Theoretical
- **Applicability**: **FOUNDATIONAL REFERENCE**. On TMFD6 with median depth = 1 lot, the granularity effect is extreme -- single-lot orders dominate. This implies that individual order events (detectable from snapshot diffs) have outsized impact.

### P25: How Markets Slowly Digest Changes in Supply and Demand

- **ID**: arXiv:0809.0822
- **Authors**: Jean-Philippe Bouchaud, J. Doyne Farmer, Fabrizio Lillo
- **Year**: 2008
- **Methodology**: Review of microstructure theory of how supply/demand fluctuations are incorporated into prices.
- **Key Findings**:
  - Revealed market liquidity is extremely low; large orders trade incrementally over long periods
  - Order flow is a highly persistent long-memory process
  - Most processed information comes from supply and demand itself, not external news
  - Square-root law of price impact: impact ~ sqrt(volume)
- **Horizon**: Theoretical
- **Applicability**: **FOUNDATIONAL**. The long-memory property means past depth changes predict future depth changes, which in turn predict price moves. This justifies rolling-window features on snapshot diffs.

---

## Synthesis: Actionable Features from Snapshot Sequences

Based on the survey, here are the specific features extractable from our L1-L5 bid/ask snapshot sequences, ordered by expected signal strength:

### Tier 1: Proven, Directly Implementable (L1-L3)

| Feature | Formula (from snapshot diffs) | Paper Basis | Complexity |
|---------|-------------------------------|-------------|------------|
| `multi_level_ofi` | Sum of (delta_bid_vol_i - delta_ask_vol_i) weighted by 1/i for i=1..5 | P09 (MLOFI) | O(1) per tick |
| `log_gofi` | log(1 + abs(ofi)) * sign(ofi), stationarized | P10 (GOFI) | O(1) per tick |
| `depth_change_velocity` | EMA of abs(delta_total_depth) over last N snapshots | P16 (Cont), P02 (Briola) | O(1) per tick |
| `symmetric_depth_mode` | delta_bid_total + delta_ask_total (liquidity provision/withdrawal) | P18 (Bouchaud) | O(1) per tick |
| `antisymmetric_depth_mode` | delta_bid_total - delta_ask_total (directional pressure) | P18 (Bouchaud) | O(1) per tick |
| `cancellation_rate_asymmetry` | (bid_depth_decreases - ask_depth_decreases) / total_decreases over window | P13, P16 | O(N) rolling |

### Tier 2: Novel, Requires Validation

| Feature | Formula | Paper Basis | Complexity |
|---------|---------|-------------|------------|
| `persistent_depth_ratio` | Fraction of depth changes that persist >= 3 snapshots | P13 (filtration) | O(N) rolling |
| `queue_depletion_rate` | Rate of L1 depth decrease (slope of depth vs time) | P16 (Cont) | O(N) rolling |
| `depth_change_inertia` | P(same-sign depth change next | current sign) | P17 (Markov) | O(N) rolling |
| `bid_ask_diff_stability` | Variance of (ask_vol_1 - bid_vol_1) over window | P07 (Siamese) | O(N) rolling |
| `level_shift_frequency` | Rate of new price levels appearing/disappearing per unit time | P24 (Farmer) | O(1) per tick |

### Tier 3: Requires Infrastructure Changes

| Feature | Requirement | Paper Basis |
|---------|-------------|-------------|
| `order_flow_image` | CNN training pipeline | P23 (Lensky) |
| `intraday_tpm_regime` | Pre-computed transition matrices by ToD | P17 (Luwang) |
| `hawkes_ofi_forecast` | Hawkes process calibration | P11 (Anantha) |

---

## Key Structural Findings

### 1. Feature Engineering > Model Architecture (P05, P02, P22)
Multiple papers converge on the finding that well-engineered features from LOB snapshots outperform complex deep learning on raw data. This validates our hand-crafted feature approach in FeatureEngine.

### 2. L1 Dominates, L2-L3 Add Incremental Value, L4-L5 Add Noise (P02, P04, P09, P14)
Consistent across all papers: L1 carries ~70% of information, L2-L3 add ~20%, L4-L5 add ~10% with noise. On thin books (TMFD6 median depth = 1 lot), deeper levels are even noisier. **Recommendation**: Weight features by 1/level_index.

### 3. Snapshot Diffs ARE Event Classification (P13, P16, P24)
Even without MBO data, snapshot differences implicitly classify events:
- `delta_depth > 0` at a level = limit order arrival
- `delta_depth < 0` at a level = cancellation OR trade execution
- New price level appearing = aggressive limit order placed inside spread
- Price level disappearing = level swept by market order (aggression)

The key is that **the sign and magnitude of depth changes at each level carry distinct information**. Depth decreases at L1 that coincide with price moves indicate trades; depth decreases at L2-L5 without price moves indicate cancellations. This distinction is extractable from snapshot sequences.

### 4. Symmetric vs. Antisymmetric Decomposition is Novel (P18)
The Bouchaud group's finding that bid-ask symmetric modes (both sides moving together) predict different things than anti-symmetric modes (one side moving opposite to the other) is underexploited. This is O(1) to compute and potentially orthogonal to existing OFI features.

### 5. Stationarization is Mandatory (P10, P22)
All features must be stationarized before use. Raw depth values or OFI are non-stationary. Log-transform, z-score, or fractional differencing are required. Our current EMA-based features partially address this.

### 6. Fleeting Orders are the Dominant Noise Source (P05, P13)
On active markets, 50-90% of depth changes are "flickering" -- orders placed and cancelled within milliseconds. Our snapshot frequency (tick updates, ~125ms median on TMFD6) naturally filters most sub-tick flickering, but we should still track persistence of depth changes.

---

## Recommended Next Steps

### Phase 1: Feature Engineering Sprint (Tier 1, ~20 LOC each)
1. **Decompose existing OFI into per-level components** (verify `ofi_depth_norm_ppm` implementation)
2. **Implement symmetric/antisymmetric depth modes** from snapshot diffs
3. **Implement log-GOFI stationarization** of OFI
4. **Implement cancellation rate asymmetry** from rolling window of depth decreases

### Phase 2: Validation (Gate Zero)
5. Measure IC at 30s, 60s, 300s horizons for each new feature on TMFD6
6. **Detrended IC mandatory** (per feedback_detrended_ic_gate.md)
7. Cross-correlation with existing features (reject if rho > 0.7 with depth_imbalance or OFI)
8. Gate Zero: >= 50 signal events/day, IC > 0.03 detrended

### Phase 3: Integration Candidates
9. Best features promoted to FeatureEngine v3
10. CBS filter candidates: persistent_depth_ratio, queue_depletion_rate

---

## References (Full List)

| ID | Short Title | Year | Key Contribution |
|----|-------------|------|------------------|
| 1808.03668 | DeepLOB | 2018 | CNN+LSTM on raw LOB snapshots |
| 2403.09267 | Deep LOB Forecasting Guide | 2024 | Microstructure-aware benchmark |
| 2502.15757 | TLOB | 2025 | Dual attention, spread-aware labels |
| 2405.18938 | HLOB | 2024 | Information persistence across levels |
| 2506.05764 | Better Inputs > Hidden Layers | 2025 | Feature eng. dominates architecture |
| 2505.02139 | LOBench | 2025 | Representation learning benchmark |
| 2505.22678 | Siamese LOB | 2025 | Bid-ask symmetry exploitation |
| 2409.02277 | Attention LOB Forecast | 2024 | Full LOB sequence forecasting |
| 1907.06230 | MLOFI | 2019 | Multi-level OFI, R-squared gains per level |
| 2112.02947 | Generalized OFI | 2021 | log-GOFI stationarization |
| 2408.03594 | Hawkes OFI Forecast | 2024 | Bid-ask cross-excitation |
| 1512.03492 | Queue Imbalance Predictor | 2016 | L1 imbalance -> next tick direction |
| 2507.22712 | Order Book Filtration | 2025 | Fleeting order filtering, persistent signal |
| 1901.10534 | LOB Feature Investigation | 2019 | Feature importance: L1 >> L2 >> L3 |
| 2409.14157 | Price Predictability LOB | 2024 | Vol vs. direction decomposition |
| 1104.4596 | Markovian LOB | 2013 | Queue depletion probability theory |
| 2601.04959 | Intraday Markov Transitions | 2026 | ToD-dependent transition matrices |
| 2405.10654 | Microstructure Modes | 2024 | Symmetric/antisymmetric decomposition |
| 2504.15908 | Spoofability Learning | 2025 | Multi-scale spoof detection |
| 2110.03687 | GRU Spoof Detection | 2021 | Spoofing from LOB sequences |
| 2602.00776 | Explainable Crypto Microstructure | 2026 | Universal feature importance across assets |
| 1810.09965 | Stationary LOB Features | 2018 | Feature stationarization for DL |
| 2304.02472 | Order Flow Image | 2023 | Spatial encoding of order flow |
| cond-mat/0210475 | Continuous Double Auction | 2002 | Order granularity > tick size |
| 0809.0822 | Markets Digest Supply/Demand | 2008 | Long-memory order flow, sqrt impact |

---

## Verdict

The literature strongly supports that **snapshot-diff features from L1-L5 bid/ask data are the primary source of short-horizon predictive information in limit order books**. The most promising NEW directions for our platform are:

1. **Symmetric/antisymmetric depth decomposition** (P18) -- novel, O(1), potentially orthogonal
2. **Log-GOFI stationarization** (P10) -- simple improvement to existing OFI
3. **Cancellation rate asymmetry** (P13, P16) -- inferred from depth decreases
4. **Persistent depth change ratio** (P13) -- filters fleeting noise

All four are computable from our existing L1-L5 snapshot data with O(1) per-tick or O(N) rolling-window complexity. No model architecture changes needed. Pure feature engineering.

# Round 22: Cross-Frequency Aggregation Survey

**Date**: 2026-03-28
**Objective**: How to aggregate tick-level LOB/microstructure features into medium-frequency (30s-5min) predictive signals
**Status**: SURVEY COMPLETE

## Executive Summary

This survey examines the "cross-frequency" or "multi-scale" problem: individual tick-level features (OFI, imbalance, spread) decay in milliseconds, but profitable trading on TXFD6/TMFD6 requires 30s+ predictions due to 4+ point RT costs. We reviewed 25+ papers across 6 methodological categories. Key findings:

1. **HAR-style multi-scale decomposition** is the most mature and practically implementable approach
2. **Dilated causal convolutions** (DeepVol, DeepLOB) provide implicit multi-scale aggregation without explicit window selection
3. **Path signatures** offer mathematically principled feature extraction but are computationally expensive
4. **Wavelet decomposition** provides clean scale separation but requires careful choice of basis
5. **Adaptive granularity** (AGA-Neural HMM) is state-of-the-art but too complex for our latency budget
6. **Double coarse-graining** (Microstructure Modes) is the most directly applicable to our OFI pipeline

**Recommended approach for implementation**: HAR-style aggregation (3 windows: 5s/30s/300s) of existing FeatureEngine features, combined with realized measures. This is O(1) per tick, requires no model training, and can be implemented in ~200 LOC.

---

## Category 1: HAR-Style Multi-Scale Models

### Paper 1.1: HAR-RV (Corsi 2009, foundational — not on arXiv)
- **Methodology**: Heterogeneous Autoregressive model uses realized volatility at daily (RV_d), weekly (RV_w), and monthly (RV_m) horizons as predictors
- **Key insight**: Different market participants operate at different frequencies; aggregating the same base measure at multiple scales captures heterogeneous information content
- **Aggregation**: Simple arithmetic mean of squared returns over non-overlapping windows
- **Applicability**: **HIGH**. Direct template for our problem. Replace RV with any feature (OFI, imbalance, spread) and use 5s/30s/300s windows instead of d/w/m

### Paper 1.2: Forecasting Realized Covariances Using HAR-type Models
- **ID**: [2412.10791](https://arxiv.org/abs/2412.10791)
- **Authors**: Quiroz, Tafakori, Manner (2024)
- **Methodology**: Extends HAR to multivariate realized covariances with attenuation bias correction
- **Key findings**: Modeling log-marginal volatilities strongly preferred; measurement error correction improves forecasts
- **Aggregation method**: Multi-scale averaging of realized variance with bias correction for microstructure noise
- **Applicability**: MEDIUM. The attenuation bias insight applies — our tick-level features contain microstructure noise that biases aggregated estimates. Log-transform before aggregation may help

### Paper 1.3: HAR-Ito Models and High-Dimensional HAR Modeling
- **ID**: [2303.02896](https://arxiv.org/abs/2303.02896)
- **Authors**: Kim, Wang (2023)
- **Methodology**: Continuous-time HAR models with Ito semimartingale theory; high-dimensional extensions
- **Key findings**: HAR structure naturally arises from continuous-time models with heterogeneous agents
- **Applicability**: LOW for direct implementation but validates the theoretical foundation of HAR aggregation

### Paper 1.4: Forecasting Realized Volatility — A Path-Dependent Perspective
- **ID**: [2503.00851](https://arxiv.org/abs/2503.00851)
- **Authors**: (2025)
- **Methodology**: Combines HAR with path-dependent volatility models (HAR-PD), using long- and short-term memory of price data
- **Key findings**: Path-dependent features capture volatility dynamics AND trend features simultaneously
- **Aggregation**: Rolling windows at multiple scales + path-dependent summary statistics
- **Applicability**: MEDIUM. Concept of path-dependent features over windows is relevant — our features should capture not just the mean but the trajectory shape

---

## Category 2: Deep Learning Multi-Scale Aggregation

### Paper 2.1: DeepVol — Volatility Forecasting from High-Frequency Data with Dilated Causal Convolutions
- **ID**: [2210.04797](https://arxiv.org/abs/2210.04797)
- **Authors**: Baldi et al. (2022)
- **Methodology**: Dilated causal convolutions directly on raw intraday data (no preprocessing) to forecast day-ahead volatility
- **Key insight**: Dilated convolutions with exponentially increasing dilation rates (1, 2, 4, 8, ...) create implicit multi-scale receptive fields without explicit window selection
- **Aggregation**: Learned via convolutional filters — the network implicitly discovers optimal aggregation weights at each scale
- **Results**: Outperforms HAR-RV and GARCH on NASDAQ-100 (2 years intraday data)
- **Applicability**: **HIGH conceptually, LOW for real-time**. We cannot run a CNN per tick. But the principle of exponentially-spaced lookback (1, 2, 4, 8, 16, 32, 64, 128, 256 ticks) informs our EMA decay rates

### Paper 2.2: DeepLOB — Deep Convolutional Neural Networks for Limit Order Books
- **ID**: [1808.03668](https://arxiv.org/abs/1808.03668)
- **Authors**: Zhang, Zohren, Roberts (2019)
- **Methodology**: CNN + Inception Module + LSTM for LOB mid-price prediction
- **Key insight**: Inception module uses parallel convolutions with different kernel sizes (1x1, 3x1, 5x1) to capture multi-scale spatial patterns in the order book
- **Aggregation**: Input is T=100 consecutive LOB snapshots; temporal aggregation via LSTM after spatial feature extraction
- **Results**: State-of-the-art on FI-2010 dataset at horizons k=10,20,50 (tick time)
- **Applicability**: MEDIUM. The lookback T=100 snapshots at 100ms = 10s history. At our 125ms tick interval, T=100 = 12.5s. Confirms that ~10s of tick history is informative

### Paper 2.3: Multi-Horizon Forecasting for Limit Order Books
- **ID**: [2105.10430](https://arxiv.org/abs/2105.10430)
- **Authors**: Briola et al. (2021)
- **Methodology**: Extends DeepLOB with encoder-decoder (Seq2Seq) and attention for multi-horizon prediction
- **Key findings**: Autoregressive decoder improves at longer horizons (k=50,100) where standard DeepLOB degrades. Alpha decay is significant — accuracy drops ~10% from k=10 to k=100
- **Aggregation**: Encoder aggregates historical LOB states; decoder generates multi-step forecasts
- **Applicability**: MEDIUM. Confirms alpha decay problem — short-horizon features lose power at longer horizons, motivating explicit multi-scale aggregation

### Paper 2.4: Deep Limit Order Book Forecasting — A Microstructural Guide
- **ID**: [2403.09267](https://arxiv.org/abs/2403.09267)
- **Authors**: Lucchese et al. (2024)
- **Methodology**: Comprehensive benchmark (LOBFrame) testing multiple deep learning architectures on NASDAQ LOB data
- **Key findings**: Rolling window volatility, OFI across depth levels, and PCA summaries of LOB snapshots are the most important engineered features. 5-day rolling z-score normalization is standard. Feature engineering > model complexity for most horizons
- **Aggregation**: Rolling window z-score (5-day), rolling volatility, multi-level OFI aggregation
- **Applicability**: **HIGH**. Confirms that engineered aggregated features (rolling vol, multi-level OFI) matter more than model sophistication

### Paper 2.5: T-KAN — Temporal Kolmogorov-Arnold Networks for LOB Forecasting
- **ID**: [2601.02310](https://arxiv.org/abs/2601.02310)
- **Authors**: Makinde (2026)
- **Methodology**: Replaces LSTM linear weights with learnable B-spline activations (KAN) for LOB prediction
- **Key findings**: 19.1% F1 improvement at k=100 horizon over DeepLOB. Interpretable spline activations show "dead zones" where features are ignored
- **Alpha decay analysis**: Explicitly quantifies alpha decay — predictive power drops exponentially with horizon. The spline dead zones reveal which feature ranges are noise at longer horizons
- **Applicability**: MEDIUM. Alpha decay quantification is useful — tells us which features survive temporal aggregation

### Paper 2.6: Exploring Microstructural Dynamics in Cryptocurrency LOBs
- **ID**: [2506.05764](https://arxiv.org/abs/2506.05764)
- **Authors**: (2025)
- **Methodology**: Compares CNN, LSTM, DeepLOB variants on crypto LOB data
- **Key finding**: **"Better inputs matter more than stacking another hidden layer"** — feature engineering (LOB depth, sampling interval, prediction horizon) has greater impact than model complexity
- **Applicability**: **HIGH**. Directly validates our approach of focusing on aggregation method rather than model architecture

---

## Category 3: Adaptive Multi-Resolution Methods

### Paper 3.1: Neural HMM with Adaptive Granularity Attention (AGA)
- **ID**: [2603.20456](https://arxiv.org/abs/2603.20456)
- **Authors**: Hu (2026)
- **Methodology**: Parallel multi-resolution encoders (dilated CNN for tick-level + wavelet-LSTM for minute-level), fused via volatility-conditioned gating mechanism
- **Architecture**:
  - Fine-grained path: dilated causal convolutions (dilation 1,2,4,8) on raw tick features
  - Coarse-grained path: learnable wavelet transform + LSTM on approximation coefficients
  - AGA gating: sigmoid gate conditioned on local volatility sigma_t and transaction frequency lambda_t
  - Fusion: g_t * h_fine + (1-g_t) * h_coarse (element-wise)
- **Key findings**:
  - 68.3% accuracy on 500ms mid-price prediction (vs 63.6% best baseline)
  - During high volatility: fine-grained features dominate (gate weight 0.72)
  - During low volatility: coarse-grained features dominate (gate weight 0.38)
  - Spearman correlation between volatility and fine-grained importance: 0.83
  - Ablation: removing AGA causes -7.2% accuracy; removing dilated conv -4.1%; removing wavelet-LSTM -2.8%
- **Aggregation**: Learned adaptive fusion of tick-level and minute-level representations
- **Applicability**: **HIGH conceptually, LOW for real-time**. The insight that volatility should modulate aggregation window is directly actionable. In high-vol regimes, use shorter EMA; in low-vol, use longer EMA. But the full model (normalizing flow + Neural HMM + attention) is far too heavy for our tick loop

### Paper 3.2: HLOB — Information Persistence and Structure in Limit Order Books
- **ID**: [2405.18938](https://arxiv.org/abs/2405.18938)
- **Authors**: (2024)
- **Methodology**: Studies how information persists across different time scales in LOB data
- **Key insight**: Information at different LOB levels persists at different rates — L1 features decay fastest, deeper levels persist longer
- **Applicability**: MEDIUM. Relevant to choosing different aggregation windows for different feature types (L1 OFI: short window; depth features: longer window)

---

## Category 4: Coarse-Graining and Temporal Aggregation

### Paper 4.1: "Microstructure Modes" — Disentangling Joint Dynamics of Prices & Order Flow
- **ID**: [2405.10654](https://arxiv.org/abs/2405.10654)
- **Authors**: Elomari-Kessab et al. (2024)
- **Methodology**: Double coarse-graining procedure to extract meaningful minute-scale information from tick data
- **Coarse-graining procedure**:
  1. Define "significant price changes" (filter out bid-ask bounce noise)
  2. Aggregate market orders, limit orders, and cancellations BETWEEN significant price changes
  3. Apply PCA to construct "microstructure modes" (symmetric and anti-symmetric flow/return patterns)
  4. Fit Vector Auto-Regressive (VAR) model on these modes
- **Key findings**: Parameters are extremely stable in time; relatively high R-squared prediction scores, especially for symmetric liquidity modes
- **Aggregation**: Event-driven aggregation (between significant price changes) rather than fixed time windows
- **Applicability**: **HIGHEST**. This is the most directly applicable paper. Our FeatureEngine already computes OFI, imbalance, spread per tick. The double coarse-graining approach:
  1. Filter ticks by "significant price change" events (e.g., mid-price moves >= 1 tick)
  2. Sum/aggregate features between these events
  3. This naturally produces variable-length windows that adapt to market activity
  4. Can be implemented as O(1) accumulator per tick

### Paper 4.2: Emergence of Randomness in Temporally Aggregated Financial Tick Sequences
- **ID**: [2511.17479](https://arxiv.org/abs/2511.17479)
- **Authors**: (2025)
- **Methodology**: Applies randomness test batteries (NIST, TestU01) to tick data at various aggregation levels
- **Key finding**: Tick-by-tick returns are highly autocorrelated and non-random; temporal aggregation progressively transforms them into random streams. There exists a critical aggregation threshold beyond which predictive structure is destroyed
- **Applicability**: **HIGH**. Directly answers our question about optimal aggregation window — there is a sweet spot between "too granular" (noisy) and "too aggregated" (signal destroyed). The paper provides a methodology (randomness test scores vs aggregation level) to find this sweet spot empirically

### Paper 4.3: Reconstruction of Order Flows Using Aggregated Data
- **ID**: [1604.02759](https://arxiv.org/abs/1604.02759)
- **Authors**: (2016)
- **Methodology**: Investigates how choices in aggregating tick-by-tick data affect quantitative model calibration
- **Key findings**: Aggregation choices (time bars vs tick bars vs volume bars) significantly affect model parameters. Volume bars produce more stable model estimates than time bars
- **Applicability**: MEDIUM. Suggests we should consider volume-weighted aggregation (aggregate N contracts of flow) rather than time-based (aggregate T seconds of flow)

---

## Category 5: Path Signatures

### Paper 5.1: Extracting Information from the Signature of a Financial Data Stream
- **ID**: [1307.7244](https://arxiv.org/abs/1307.7244)
- **Authors**: Gyurko, Lyons, Sherwood (2013)
- **Methodology**: Uses truncated path signatures from rough path theory to encode financial time series into fixed-dimensional feature vectors
- **Key insight**: The signature of a path X:[0,T] -> R^d is a sequence of iterated integrals that uniquely characterizes the path up to tree-like equivalence. Truncated to level k, it captures all information up to k-th order interactions between dimensions
- **Signature computation**: For a 2D path (price, volume), level-2 signature = (integral dp, integral dv, integral p dp, integral v dv, integral p dv, integral v dp) — 6 features capturing mean, variance, and lead-lag relationships
- **Results**: Successfully classifies market regimes and detects atypical behavior in WTI crude oil futures
- **Applicability**: **HIGH conceptually**. Path signatures provide a principled way to summarize a window of tick data into fixed-dimensional features that capture order (sequence), cross-correlations, and higher-order dynamics. Level-2 signature of our 21-feature engine over a 30s window would produce ~250 features. Level-3 would produce ~3000. Computationally feasible if done per 30s window (not per tick)

### Paper 5.2: Path Signatures for Feature Extraction
- **ID**: [2506.01815](https://arxiv.org/abs/2506.01815)
- **Authors**: (2025)
- **Methodology**: Comprehensive review of path signatures as feature extraction for time series
- **Key findings**: Signatures are universal nonlinear functionals of paths; they capture sequential structure that summary statistics (mean, std) miss. Particularly effective for irregular time series (like tick data)
- **Applicability**: MEDIUM. Good reference for implementation details

### Paper 5.3: Signature-Informed Transformer for Asset Allocation
- **ID**: [2510.03129](https://arxiv.org/abs/2510.03129)
- **Authors**: (2025)
- **Methodology**: Uses path signatures as input features to transformer for portfolio optimization
- **Key findings**: Signature features improve transformer performance by providing temporally-aware representations
- **Applicability**: LOW directly, but validates signature features in financial ML

### Paper 5.4: Volatility Modeling with Rough Paths — A Signature-Based Alternative
- **ID**: [2507.23392](https://arxiv.org/abs/2507.23392)
- **Authors**: (2025)
- **Methodology**: Models volatility as a linear functional of the signature of a stochastic process
- **Key findings**: Achieves calibration accuracy comparable to asymptotic expansion while being more flexible. Works with rough Bergomi models
- **Applicability**: LOW for direct use, but the idea of "realized signature" (signature computed over a rolling window of observed data) as a volatility predictor is interesting

---

## Category 6: Order Flow and OFI Aggregation

### Paper 6.1: Price Impact of Order Flow Imbalance — Multi-level, Cross-sectional and Forecasting
- **ID**: [2112.13213](https://arxiv.org/abs/2112.13213)
- **Authors**: Xu et al. (2021)
- **Methodology**: Multi-level OFI aggregation across order book depth levels; cross-sectional OFI impact
- **Key findings**:
  - OFI at deeper levels (L2-L5) adds explanatory power for price impact beyond L1
  - PCA-integrated OFI across levels outperforms single-level OFI
  - Cross-sectional OFI (from correlated assets) adds further predictive power
  - **Out-of-sample performance degrades when including too many levels** — there is an optimal depth
- **Aggregation**: PCA across depth levels; time-windowed aggregation (1-min, 5-min, etc.)
- **Applicability**: **HIGH**. We already compute L1 OFI. Adding L2-L5 depth OFI with PCA integration is feasible. The finding that more levels eventually hurt OOS is critical — matches our R11/R18 experience

### Paper 6.2: Stochastic Price Dynamics in Response to OFI — CSI 300 Index Futures
- **ID**: [2505.17388](https://arxiv.org/abs/2505.17388)
- **Authors**: (2025)
- **Methodology**: Systematic 2D parameter sweep: aggregation window x forecast horizon for OFI on futures tick data
- **Key findings**: Creates a 2D heatmap of OFI predictive power across (aggregation_window, forecast_horizon) space. LASSO regression on historical OFI values over varying time windows
- **Aggregation**: Rolling sum of OFI over windows from seconds to minutes; backtested on 1 year of tick data
- **Applicability**: **HIGHEST for our specific use case**. This is exactly our problem — aggregating tick-level OFI on index futures at various windows. We should replicate their 2D sweep on TXFD6/TMFD6 data

### Paper 6.3: Forecasting High Frequency Order Flow Imbalance Using Hawkes Processes
- **ID**: [2408.03594](https://arxiv.org/abs/2408.03594)
- **Authors**: (2024)
- **Methodology**: Uses Hawkes processes to model self-exciting order flow; computes OFI at tick time without classification algorithms
- **Key findings**: Tick-time OFI (updated per event) preserves clustering structure that time-aggregated OFI misses. Aggregation destroys information about arrival rate clustering
- **Aggregation**: Event-time (per tick) rather than clock-time; Hawkes process captures temporal dependencies without explicit windowing
- **Applicability**: MEDIUM. Confirms that fixed time-window aggregation loses information. Event-driven aggregation (Paper 4.1) is preferable

### Paper 6.4: Returns and Order Flow Imbalances — Intraday Dynamics
- **ID**: [2508.06788](https://arxiv.org/abs/2508.06788)
- **Authors**: (2025)
- **Methodology**: Analyzes intraday OFI dynamics and macroeconomic news effects
- **Key findings**: OFI-return relationship varies throughout the trading day and around news events
- **Applicability**: LOW directly, but confirms time-of-day modulation matters (consistent with our CBS ToD gating)

### Paper 6.5: Microstructure-Empowered Stock Factor Extraction and Utilization
- **ID**: [2308.08135](https://arxiv.org/abs/2308.08135)
- **Authors**: Jiao et al. (2023)
- **Methodology**: Framework to extract daily stock factors from tick-level order flow data using learned aggregation
- **Key findings**:
  - Learns to aggregate tick-level features into daily factors via attention mechanism
  - Handles full year of order flow data across multiple stocks
  - Extracted factors improve both trend prediction and order execution
  - **Adaptability across temporal granularities** is a key design goal
- **Aggregation**: Learned attention-weighted aggregation of tick-level features over intraday windows
- **Applicability**: **HIGH**. This is the tick-to-daily version of our exact problem. Their framework of "compute tick features -> aggregate to target frequency via attention" is the template. For our case: compute 21 features per tick -> aggregate to 30s/300s via learned or fixed weights

### Paper 6.6: Learning to Predict Short-Term Volatility with Order Flow Image Representation
- **ID**: [2304.02472](https://arxiv.org/abs/2304.02472)
- **Authors**: Lensky, Hao (2023)
- **Methodology**: Transforms order flow over fixed windows into images (trade size/direction/LOB mapped to RGB channels), then CNN prediction
- **Key findings**: Simple 3-layer CNN on order flow images outperforms more complex models. Aggregated features supplement raw images
- **Aggregation**: Fixed time-window snapshots converted to 2D image representations
- **Applicability**: LOW for real-time (image generation per window is expensive), but the concept of "order flow snapshot as multi-channel image" is interesting for offline analysis

### Paper 6.7: Order Book Filtration and Directional Signal Extraction at High Frequency
- **ID**: [2507.22712](https://arxiv.org/abs/2507.22712)
- **Authors**: (2025)
- **Methodology**: Filters LOB events by order lifetime, update count, and inter-update delay before computing OBI (order book imbalance)
- **Key findings**: Structurally filtering events (removing short-lived "flickering" orders) before computing signals improves directional prediction
- **Aggregation**: Pre-aggregation filtering of noisy events, then standard OBI computation
- **Applicability**: **HIGH**. Before aggregating our features, we should filter out flickering/spoofed quotes. This is a preprocessing step that improves all downstream aggregation methods

---

## Category 7: Wavelet Methods

### Paper 7.1: MultiWave — Multiresolution Deep Architectures through Wavelet Decomposition
- **ID**: [2306.10164](https://arxiv.org/abs/2306.10164)
- **Authors**: (2023)
- **Methodology**: Decomposes each signal into subsignals of varying frequencies using wavelets; groups into frequency bands handled by different model components
- **Applicability**: MEDIUM. The decomposition concept is relevant but full implementation is heavy

### Paper 7.2: Multi-Order Wavelet Derivative Transform for Time Series Forecasting
- **ID**: [2505.11781](https://arxiv.org/abs/2505.11781)
- **Authors**: (2025)
- **Methodology**: Wavelet transform on derivatives of the series to capture rate-of-change and regime shifts
- **Applicability**: LOW directly, but the idea of applying wavelets to feature derivatives (rate of change of OFI, not OFI itself) is interesting

---

## Synthesis: Actionable Aggregation Methods for Our Pipeline

### Method A: HAR-Style Fixed Multi-Window Aggregation (RECOMMENDED FIRST)

**Complexity**: O(1) per tick using exponential moving averages
**Implementation**: ~100 LOC in FeatureEngine

For each of our 21 features f_i, maintain 3 EMAs:
```
f_i_fast  = EMA(f_i, alpha=2/(5s_ticks+1))     # ~5s half-life (~40 ticks)
f_i_med   = EMA(f_i, alpha=2/(30s_ticks+1))    # ~30s half-life (~240 ticks)
f_i_slow  = EMA(f_i, alpha=2/(300s_ticks+1))   # ~5min half-life (~2400 ticks)
```

This produces 63 aggregated features (21 x 3 windows). Add cross-scale features:
```
f_i_fast_minus_slow = f_i_fast - f_i_slow       # momentum indicator
f_i_fast_over_slow  = f_i_fast / f_i_slow       # relative strength
```

Total: 21 + 63 + 42 = 126 features, all O(1) per tick.

**Evidence**: HAR literature (Papers 1.1-1.4) shows multi-scale averaging captures heterogeneous agent dynamics. DeepVol (Paper 2.1) shows exponentially-spaced windows match dilated convolution receptive fields. AGA (Paper 3.1) shows this works for microstructure data.

### Method B: Realized Measures Aggregation

**Complexity**: O(1) per tick using accumulators
**Implementation**: ~80 LOC

Per rolling window (30s, 300s), compute:
```
RV_w       = sum(r_i^2)                          # realized variance
RQ_w       = sum(r_i^4)                          # realized quarticity (for noise)
OFI_cum_w  = sum(ofi_i)                          # cumulative OFI
OFI_abs_w  = sum(|ofi_i|)                        # OFI activity
imb_mean_w = mean(imbalance_i)                   # average imbalance
imb_std_w  = std(imbalance_i)                    # imbalance volatility
spread_max_w = max(spread_i)                     # max spread in window
tick_count_w = count(ticks)                       # activity rate
```

**Evidence**: Papers 1.1-1.3 (HAR/RV literature), Paper 4.2 (aggregation threshold), Paper 6.2 (OFI aggregation on futures).

### Method C: Event-Driven Coarse-Graining (Paper 4.1)

**Complexity**: O(1) per tick with event-driven reset
**Implementation**: ~120 LOC

Instead of fixed windows, aggregate features between "significant price changes":
```python
class EventAggregator:
    def on_tick(self, tick):
        self.accum_ofi += tick.ofi
        self.accum_volume += tick.volume
        self.tick_count += 1

        if abs(self.mid_price - self.last_event_mid) >= MIN_MOVE:
            # Emit aggregated features
            emit(self.accum_ofi, self.accum_volume, self.tick_count, ...)
            self.reset()
```

This naturally adapts window length to market activity: during volatile periods, events fire frequently (short effective window); during calm periods, events fire rarely (long effective window).

**Evidence**: Paper 4.1 (Microstructure Modes), Paper 4.3 (volume bars > time bars), Paper 3.1 (volatility-adaptive gating).

### Method D: Volatility-Adaptive EMA (Derived from Paper 3.1)

**Complexity**: O(1) per tick
**Implementation**: ~50 LOC on top of Method A

Modulate EMA decay rate by local volatility:
```python
# High volatility -> shorter EMA (trust recent data more)
# Low volatility -> longer EMA (smooth more)
adaptive_alpha = base_alpha * (1 + k * sigma_local / sigma_baseline)
```

**Evidence**: Paper 3.1 shows Spearman correlation of 0.83 between volatility and optimal fine-grained weight. Paper 2.1 confirms volatility-adaptive aggregation outperforms fixed windows.

### Method E: Path Signatures (For offline/research only)

**Complexity**: O(d^k * T) per window, where d=features, k=signature level, T=ticks in window
**Implementation**: Use `signatory` or `iisignature` Python library

Compute level-2 signature of (price, ofi, imbalance, spread) over 30s window:
- 4 level-1 terms (means)
- 16 level-2 terms (cross-correlations and lead-lag)
- Total: 20 features per window

**Evidence**: Papers 5.1-5.4. Theoretically powerful but computationally expensive. Reserve for offline feature discovery, not real-time.

---

## Optimal Window Recommendations by Feature Type

Based on the literature synthesis:

| Feature Type | Fast Window | Medium Window | Slow Window | Rationale |
|---|---|---|---|---|
| OFI (L1) | 5s | 30s | 300s | Decays as OU process (tau~15s, R19). 5s captures momentum, 30s the mean-reversion scale |
| Depth imbalance | 10s | 60s | 600s | Deeper features persist longer (Paper 3.2) |
| Spread | 30s | 300s | 900s | Spread regime changes are slow (R16 finding) |
| Realized volatility | 30s | 300s | 1800s | HAR literature standard; RV at multiple scales captures heterogeneous traders |
| Trade intensity | 5s | 30s | 300s | Self-exciting (Hawkes), fast decay |
| Mid-price return | 1s | 10s | 60s | Most ephemeral signal; beyond 60s is noise (Paper 4.2) |

---

## Implementation Priority

1. **Phase 1 (NOW)**: Method A — HAR-style 3-window EMAs on all 21 features. ~100 LOC, O(1), zero model risk
2. **Phase 2 (+1 week)**: Method B — Realized measures (RV, cumulative OFI, tick count) at 30s/300s. ~80 LOC
3. **Phase 3 (+2 weeks)**: Method C — Event-driven aggregation between significant price changes. ~120 LOC
4. **Phase 4 (Research)**: Method D — Volatility-adaptive EMA using sigma_local from Method B
5. **Phase 5 (Research)**: Method E — Path signatures for offline feature discovery; promote survivors to online computation

---

## Key Negative Results and Warnings

1. **Aggregation destroys information** (Paper 4.2): There is a critical aggregation threshold beyond which predictive structure is destroyed. Over-smoothing is worse than under-smoothing
2. **More levels eventually hurt OOS** (Paper 6.1): Adding L3-L5 depth OFI improved in-sample but degraded out-of-sample after a certain depth. Matches our R11 finding
3. **Alpha decay is exponential** (Papers 2.3, 2.5): Predictive power drops exponentially with horizon. Features informative at 500ms may be pure noise at 30s. Must validate each aggregated feature's IC at the TARGET horizon
4. **Fixed windows lose clustering structure** (Paper 6.3): Hawkes-type arrival patterns are destroyed by fixed time windows. Event-driven aggregation preserves them
5. **Microstructure noise biases estimates** (Paper 1.2): Raw squared returns overestimate realized variance due to bid-ask bounce. Use bias-corrected estimators or filter ticks first (Paper 6.7)
6. **Our detrended IC gate still applies**: If an aggregated feature shows monotonically increasing IC with window size, it is likely capturing trend, not microstructure (our R18 finding)

---

## References Summary

| ID | Short Title | Category | Applicability |
|---|---|---|---|
| Corsi 2009 | HAR-RV | HAR | HIGHEST |
| 2412.10791 | HAR Covariance Forecasting | HAR | MEDIUM |
| 2303.02896 | HAR-Ito | HAR | LOW |
| 2503.00851 | HAR Path-Dependent | HAR | MEDIUM |
| 2210.04797 | DeepVol (Dilated Conv) | Deep Learning | HIGH concept |
| 1808.03668 | DeepLOB | Deep Learning | MEDIUM |
| 2105.10430 | Multi-Horizon LOB | Deep Learning | MEDIUM |
| 2403.09267 | LOBFrame Benchmark | Deep Learning | HIGH |
| 2601.02310 | T-KAN LOB | Deep Learning | MEDIUM |
| 2506.05764 | Crypto LOB Inputs | Deep Learning | HIGH |
| 2603.20456 | AGA Neural HMM | Adaptive | HIGH concept |
| 2405.18938 | HLOB Persistence | Adaptive | MEDIUM |
| 2405.10654 | Microstructure Modes | Coarse-Graining | HIGHEST |
| 2511.17479 | Emergence of Randomness | Coarse-Graining | HIGH |
| 1604.02759 | Order Flow Reconstruction | Coarse-Graining | MEDIUM |
| 1307.7244 | Path Signatures Finance | Signatures | HIGH concept |
| 2506.01815 | Path Sig Feature Extract | Signatures | MEDIUM |
| 2510.03129 | Signature Transformer | Signatures | LOW |
| 2507.23392 | Rough Path Volatility | Signatures | LOW |
| 2112.13213 | Multi-level OFI | OFI/Aggregation | HIGH |
| 2505.17388 | CSI 300 OFI Windows | OFI/Aggregation | HIGHEST |
| 2408.03594 | Hawkes OFI Forecasting | OFI/Aggregation | MEDIUM |
| 2308.08135 | Microstructure Factor Extract | OFI/Aggregation | HIGH |
| 2304.02472 | Order Flow Images | OFI/Aggregation | LOW |
| 2507.22712 | LOB Filtration | OFI/Aggregation | HIGH |
| 2306.10164 | MultiWave | Wavelet | MEDIUM |
| 2505.11781 | Wavelet Derivative Transform | Wavelet | LOW |

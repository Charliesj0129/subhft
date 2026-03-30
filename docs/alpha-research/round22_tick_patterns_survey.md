# Round 22: Tick-Level Pattern & Point Process Survey

**Date**: 2026-03-28
**Scope**: Extracting predictive signals from tick-by-tick trade/quote data -- inter-arrival times, tick clustering, trade intensity, and price formation at the tick level.
**Queries**: 10 arXiv queries + 6 targeted web searches. ~26 unique relevant papers identified.

## Executive Summary

This survey examines whether tick-level temporal patterns (inter-arrival times, clustering, intensity dynamics) can generate tradeable signals at 30s+ horizons on TAIFEX futures. The literature is rich in Hawkes process and point process models for LOB dynamics, but **almost universally focused on sub-second to few-second horizons**. The key tension for our use case: the most informative tick patterns decay within seconds, while our cost structure demands 30s+ holding periods.

**Bottom-line assessment**: Three directions show potential for our constraints:

1. **Hawkes branching ratio as slow regime indicator** (CONDITIONAL GO) -- branching ratio evolves over minutes, not ticks
2. **Tick intensity as volatility forecaster** (CONDITIONAL GO) -- realized tick-rate predicts 1-5 min volatility
3. **Duration-based trade classification proxy** (INVESTIGATE) -- infer informed trading from inter-arrival patterns without buy/sell labels

Everything else either requires trade classification we lack, operates at sub-second horizons, or has been shown to not extend beyond seconds.

---

## Section A: Hawkes Processes for LOB Dynamics

### A1. Hawkes processes in finance (Survey)
- **ID**: `1502.04592v2`
- **Authors**: Bacry, Mastromatteo, Muzy (2015)
- **Methodology**: Comprehensive survey of Hawkes process applications in HF finance. Covers self/mutual excitation, kernel estimation, branching ratio, volatility estimation, market stability.
- **Key findings**:
  - Hawkes processes capture the empirical fact that ~80% of market events are endogenous (triggered by prior events, not external news)
  - Branching ratio n (ratio of endogenous to total events) consistently found near 0.8-0.95 in liquid markets
  - Volatility at transaction level can be expressed as sigma^2 = sigma_0^2 / (1 - n)^2, directly linking intensity to volatility
  - Power-law kernels fit better than exponential, but exponential is computationally tractable
- **Horizon**: Sub-second to minutes (theory); daily (branching ratio estimation)
- **Data requirements**: Tick timestamps, prices. Trade classification helpful but not required for univariate models.
- **Applicability**: **HIGH**. Univariate Hawkes on tick timestamps is directly computable from our data. Branching ratio n is a slow-moving regime indicator.

### A2. Unified theory of order flow, market impact, and volatility
- **ID**: `2601.23172v2`
- **Authors**: Muhle-Karbe, Ouazzani Chahdi, Rosenbaum, Szymanski (2026)
- **Methodology**: Distinguishes "core orders" from "reaction flow", both as Hawkes processes. Derives scaling limit connecting order flow persistence (H_0), rough volume/volatility (Hurst H ~ 0.1), and power-law impact.
- **Key findings**:
  - All key market quantities pinned by single statistic H_0 ~ 3/4 (core flow persistence)
  - Volatility roughness H = 2*H_0 - 3/2 ~ 0 (very rough)
  - Signed order flow = fractional process + martingale
  - Impact follows power law with exponent 2 - 2*H_0 ~ 0.5 (square-root law)
- **Horizon**: Multi-scale (theory). Calibrated from tick data but predictions span minutes to hours.
- **Data requirements**: Signed order flow (needs trade classification)
- **Applicability**: **MEDIUM**. Theoretical framework is elegant but requires signed flow. The H_0 estimation from unsigned flow might be feasible as regime indicator.

### A3. General Compound Hawkes Processes for Mid-Price Prediction
- **ID**: `2110.07075v1`
- **Authors**: Sjogren, DeLise (2021)
- **Methodology**: Extends General Compound Hawkes Process (GCHP) from LOB simulation to mid-price prediction. Models inter-arrival times AND jump sizes jointly.
- **Key findings**:
  - GCHP provides both direction and volatility predictions
  - Applied to futures (E-mini S&P, corn) and stocks
  - Prediction accuracy for direction: ~52-55% at short horizons
  - Volatility prediction: better than GARCH at sub-minute scales
  - Decays rapidly beyond ~30s
- **Horizon**: 1s to 30s (useful signal range)
- **Data requirements**: Tick timestamps, mid-price changes. No trade classification needed.
- **Applicability**: **MEDIUM-LOW**. Prediction decays right at our minimum horizon. The volatility prediction component might extend further.

### A4. Compound Hawkes Processes in Limit Order Books
- **ID**: `1712.03106v1`
- **Authors**: Swishchuk, Remillard, Elliott, Chavez-Casillas (2017)
- **Methodology**: Two models: compound and regime-switching compound Hawkes. LLN and FCLT for price processes. Links volatility to order flow parameters.
- **Key findings**:
  - Price volatility decomposed into: baseline intensity * mean jump^2 + intensity * jump variance + self-excitation contribution
  - Regime-switching variant captures intraday patterns (opening, lunch, close)
  - Diffusion limit provides closed-form volatility expression in terms of Hawkes parameters
- **Horizon**: Intraday regime-level (minutes to hours for regime identification)
- **Data requirements**: Tick prices, timestamps. No trade classification needed for basic model.
- **Applicability**: **MEDIUM**. Regime-switching component could identify high/low volatility regimes usable as CBS filter. Volatility formula is actionable.

### A5. Event-Based LOB Simulation with Neural Hawkes
- **ID**: `2502.17417v1`
- **Authors**: Lalor, Swishchuk (2025)
- **Methodology**: Neural Hawkes process (LSTM-based intensity) models 12 LOB event types. Builds mid-price simulator for RL market-making.
- **Key findings**:
  - Neural Hawkes captures long/short-term event dependencies better than parametric Hawkes
  - Simulated mid-price volatility matches real data
  - Trade fill distributions align with real execution
  - Computationally expensive (LSTM per event)
- **Horizon**: Event-by-event (sub-second simulation)
- **Data requirements**: Full L3 order book (12 event types). We only have L1-L5 snapshots.
- **Applicability**: **LOW**. Requires L3 data we do not have. Simulation-focused, not prediction-focused.

### A6. Non-parametric Estimation of Quadratic Hawkes (Order Book Events)
- **ID**: `2005.05730v1`
- **Authors**: Fosset, Bouchaud, Benzaquen (2020)
- **Methodology**: Quadratic Hawkes encodes influence of past price changes on future events (not just past events on future events). Non-parametric kernel calibration.
- **Key findings**:
  - Quadratic kernel = diagonal (past volatility effect) + rank-one "Zumbach" (past trend effect)
  - All kernels are power-law in time
  - Exogenous event rate is tiny fraction (~5%) of total rate -- market is 95% endogenous
  - System operates near critical point (stronger feedback = instability)
  - **Zumbach effect**: past trends increase future activity rate
- **Horizon**: Kernel decays as power law, so influence spans seconds to minutes
- **Data requirements**: Market orders, limit orders, cancellations (L3 data ideal). Can approximate with tick stream.
- **Applicability**: **MEDIUM-HIGH**. The Zumbach effect (past trends -> future activity) is directly relevant. Can proxy with tick-rate acceleration after price moves. Power-law decay means signal persists into our horizon.

### A7. Endogenous Liquidity Crises
- **ID**: `1912.00359v2`
- **Authors**: Fosset, Bouchaud, Benzaquen (2019)
- **Methodology**: Studies feedback between past price volatility/trends and future liquidity provision. Demonstrates phase transition between stable and unstable regimes in stylized order book model.
- **Key findings**:
  - Liquidity decreases with amplitude of past volatility and price trends
  - Feedback mechanism: less liquidity -> more volatility -> even less liquidity
  - Second-order phase transition: weak feedback = stable, strong feedback = liquidity crises with probability 1
  - Critical exponents belong to new universality class
  - Non-linear Hawkes shows "activated" crises without being at instability edge
- **Horizon**: Crisis dynamics unfold over minutes to hours
- **Data requirements**: Tick prices, L1 depth (we have both)
- **Applicability**: **MEDIUM**. Detecting proximity to liquidity crisis regime could be a valuable risk filter for CBS/strategy activation. Monitor tick-rate acceleration + depth thinning jointly.

### A8. Queue-reactive Hawkes models for the order flow
- **ID**: `1901.08938`
- **Authors**: Wu, Rambaldi, Muzy, Bacry (2019)
- **Methodology**: Combines queue-reactive (state-dependent) rates with Hawkes self-excitation. Two variants tested on Eurex futures (Bund, DAX).
- **Key findings**:
  - Queue state (current depth) dramatically affects event arrival rates
  - Hawkes component improves pure queue-reactive model for inter-event time statistics AND queue distributions
  - State + history jointly determine intensity better than either alone
  - Calibrated on futures data directly relevant to our setting
- **Horizon**: Event-level (sub-second to seconds)
- **Data requirements**: L1 queue sizes + event timestamps. We have L1-L5 depth.
- **Applicability**: **MEDIUM**. We have queue sizes. The insight that queue depth conditions Hawkes intensity is actionable. Low queue + high Hawkes intensity = adverse selection signal.

---

## Section B: Tick Intensity and Volatility

### B1. Marked Hawkes process modeling of price dynamics and volatility estimation
- **ID**: `1907.12025`
- **Authors**: Lee, Seo (2019). Published: Journal of Empirical Finance, vol. 40, pp. 174-200.
- **Methodology**: Marked Hawkes model for tick-level price dynamics with random marks (jump sizes). Derives volatility formula from stochastic/statistical methods.
- **Key findings**:
  - Hawkes volatility formula captures intraday volatility dynamics
  - Marks (price change sizes) depend on intensity state
  - Larger jumps during high-intensity periods (informed trading signature)
  - Comparable to realized volatility at 5-min aggregation
- **Horizon**: 5-minute to intraday
- **Data requirements**: Tick prices, timestamps. No trade classification needed.
- **Applicability**: **HIGH**. Directly applicable. Tick-level Hawkes volatility estimation at 5-min scale is within our target. No classification needed.

### B2. Application of Hawkes volatility in filtered HF price process in tick structures
- **ID**: `2207.05939`
- **Authors**: Lee (2022, revised 2024). Published: Applied Stochastic Models in Business and Industry (2024).
- **Methodology**: Derives variance formula for both unmarked and marked Hawkes models. Applies to mid-price filtered at 0.1s intervals.
- **Key findings**:
  - Variance formula directly applicable under general Hawkes settings
  - Linear impact function + mark-intensity dependency handled
  - Applied to mid-price at 0.1s filtering -- shows reliable intraday volatility estimates
  - Expected to have "high utilization in real-time risk management"
- **Horizon**: 0.1s to intraday
- **Data requirements**: Mid-price stream (tick timestamps). We have this.
- **Applicability**: **HIGH**. Real-time Hawkes-based volatility at 0.1s resolution is directly implementable. Can use as CBS volatility filter.

### B3. Volatility is rough
- **ID**: `1410.3394v1`
- **Authors**: Gatheral, Jaisson, Rosenbaum (2014)
- **Methodology**: Estimates Hurst exponent of log-volatility from HF data. Finds H ~ 0.1 across assets and timescales. Proposes Rough FSV model.
- **Key findings**:
  - Log-volatility behaves as fractional Brownian motion with H ~ 0.1
  - Improved forecasts of realized volatility vs. standard models
  - Microstructural foundation: HF trading and order splitting create rough volatility
  - Classical tests wrongly detect "long memory" in rough processes
- **Horizon**: Multi-scale (1 min to daily)
- **Data requirements**: HF price data for realized volatility estimation
- **Applicability**: **MEDIUM**. The roughness of volatility means short-horizon vol estimates are highly informative for slightly-longer-horizon forecasts. Actionable via RV estimation at multiple scales.

### B4. Microstructural foundations of leverage effect and rough volatility
- **ID**: `1609.05177v1`
- **Authors**: El Euch, Fukasawa, Rosenbaum (2016)
- **Methodology**: Builds microscopic Hawkes model encoding: high endogeneity, no-arbitrage, buy/sell asymmetry, metaorders. Proves scaling limit = rough Heston.
- **Key findings**:
  - Endogeneity + no-arbitrage -> Heston (leverage effect)
  - + Metaorders -> Rough Heston (rough volatility)
  - Market microstructure IS the origin of rough volatility and leverage
  - Branching ratio near 1 is necessary for these properties
- **Horizon**: Theoretical (scaling limit)
- **Data requirements**: Tick data for Hawkes calibration
- **Applicability**: **LOW** (theory-focused). But confirms that branching ratio estimation is meaningful.

### B5. Unbounded intensity model for point processes
- **ID**: `2408.06519v2`
- **Authors**: Christensen, Kolokolov (2024)
- **Methodology**: Models intensity that can be locally unbounded (intensity bursts). Non-parametric detection of burst events. Applied to EUR/USD.
- **Key findings**:
  - Intensity bursts capture abnormal surges in trading activity
  - Bursts positively related to: volatility, illiquidity, drift bursts (price jumps)
  - Effect reinforced when order flow is imbalanced or LOB elasticity is large
  - Detects "nontrivial amount" of intensity bursts in FX data
  - Heavy traffic condition enables inference from finite intervals
- **Horizon**: Event detection (real-time), effects span minutes
- **Data requirements**: Tick timestamps. No classification needed.
- **Applicability**: **HIGH**. Intensity burst detection = regime change signal. Directly computable from our tick stream. Bursts predict volatility and illiquidity -- actionable for CBS risk filter.

### B6. Tick-by-tick price model with mean-field interaction
- **ID**: `2504.03445`
- **Authors**: Dai Pra, Pigato (2026)
- **Methodology**: Agent-based model where buy/sell orders = Hawkes processes with mean-field interaction. Large-scale limit at critical parameters.
- **Key findings**:
  - At critical parameters, aggregated price -> stochastic volatility with leverage
  - Mean-field interaction produces herd behavior and contagion
  - Faster-than-linear mean reversion of volatility process
  - Positive correlations between agent volumes reflect real market features
- **Horizon**: Theoretical (scaling limit)
- **Data requirements**: N/A (theoretical)
- **Applicability**: **LOW**. Theoretical, but confirms that tick-level Hawkes statistics aggregate to meaningful volatility dynamics.

---

## Section C: Spread and Event Prediction

### C1. Self-exciting nature of bid-ask spread dynamics
- **ID**: `2303.02038v2`
- **Authors**: Ruan, Bacry, Muzy (2023)
- **Methodology**: State-Dependent Spread Hawkes (SDSH) model. Spread jump sizes vary; current spread state affects intensity. Applied to CAC40 Euronext.
- **Key findings**:
  - Spread dynamics are self-exciting (spread widening -> more spread changes)
  - State-dependency crucial: intensity depends on current spread level
  - Captures spread distributions, inter-event times, autocorrelations
  - **Short-term spread forecasting possible**
- **Horizon**: Sub-second to seconds (spread prediction)
- **Data requirements**: Best bid/ask prices, timestamps. We have this.
- **Applicability**: **MEDIUM**. Spread prediction at short horizons. Could be useful for execution timing but not for alpha at 30s+.

### C2. LOB Event Stream Prediction with Diffusion Model
- **ID**: `2412.09631v1`
- **Authors**: Zheng, Li, Ouyang, Liang, Shao (2024)
- **Methodology**: LOBDIF -- diffusion model learns time-event joint distribution. Decomposes into Gaussian steps. Skip-step sampling for speed.
- **Key findings**:
  - Outperforms Hawkes and Neural Hawkes on event prediction
  - Models both timing and type of next LOB event
  - Tested on 3 real-world assets
  - Novel paradigm vs. traditional point processes
- **Horizon**: Next-event prediction (sub-second)
- **Data requirements**: L3 order book events (individual orders)
- **Applicability**: **LOW**. Requires L3 data. Next-event horizon too short.

### C3. Marked point processes and intensity ratios for LOB modeling
- **ID**: `2001.08442v1`
- **Authors**: Muni Toke, Yoshida (2020)
- **Methodology**: Intensity ratio model with three multiplicative components: baseline, state-dependent, mark-dependent. Predicts market order sign and aggressiveness.
- **Key findings**:
  - Imbalance and spread are the most significant state-dependent signals
  - **Outperforms pure Hawkes methods** for predicting sign and aggressiveness of market orders
  - Marked ratio model captures both state-dependency and clustering
  - Calibrated on Euronext Paris high-frequency data
- **Horizon**: Next-event to few seconds
- **Data requirements**: L1 quotes, market orders with classification
- **Applicability**: **MEDIUM**. The finding that imbalance + spread + clustering jointly predict order sign is actionable. We already have imbalance features. Adding tick-rate could improve.

### C4. Forecasting High Frequency Order Flow Imbalance with Hawkes
- **ID**: `2408.03594v1`
- **Authors**: Anantha, Jain (2024)
- **Methodology**: Hawkes processes estimate OFI while accounting for lagged bid/offer dependence. Forecasts near-term OFI distribution.
- **Key findings**:
  - Sum of Exponentials kernel gives best OFI forecast
  - OFI forecasted via Hawkes outperforms VAR and raw counting
  - Applied to NSE (India) tick data
  - Near-term distribution enables probabilistic trading signals
- **Horizon**: Seconds to ~1 minute
- **Data requirements**: Tick-by-tick bid/ask events (we have bid/ask snapshots)
- **Applicability**: **MEDIUM**. OFI forecasting from Hawkes could enhance our existing OFI features. But requires reconstructing event-level OFI from snapshots.

### C5. Event-Time Anchor Selection for Multi-Contract Quoting
- **ID**: `2507.05749v2`
- **Authors**: Anantha, Jain, Goyal, Misra (2025)
- **Methodology**: Contrasts Hawkes-based order flow forecasts with Composite Liquidity Factor (CLF) from LOB shape for reference-contract selection.
- **Key findings**:
  - Event-history and LOB-state signals are complementary for execution risk
  - Applied to NIFTY futures pair -- similar futures market to ours
  - Hawkes forecasts provide temporal view, LOB shape provides instantaneous view
- **Horizon**: Short execution windows (seconds)
- **Data requirements**: Tick-by-tick data for multiple contracts
- **Applicability**: **LOW**. Multi-contract execution optimization, not alpha generation.

### C6. LOBERT: Foundation Model for LOB Messages
- **ID**: `2511.12563v1`
- **Authors**: Linna, Baltakys, Iosifidis, Kanniainen (2025)
- **Methodology**: BERT-adapted encoder for LOB data. Multi-dimensional messages as single tokens with continuous price/volume/time.
- **Key findings**:
  - Leading performance on mid-price movement prediction
  - Reduced context length vs. previous deep learning methods
  - General-purpose -- fine-tunable for multiple tasks
  - Requires L3 message-level data
- **Horizon**: Next-event to seconds
- **Data requirements**: L3 order book messages
- **Applicability**: **LOW**. Requires L3 data. Black-box model.

---

## Section D: Duration Models (ACD)

### D1. Hierarchical Semi-parametric Duration Models
- **ID**: `1403.0998v1`
- **Authors**: Tang, Schervish (2014)
- **Methodology**: Semi-parametric model for trade inter-arrival times. Non-parametric recent past + parametric distant past. Online learning for intraday trends.
- **Key findings**:
  - Outperforms ACD family in prediction log-likelihood and diagnostics (NYSE data)
  - Recent past effects are non-parametric (flexible shape), distant past is ARMA-like
  - Online learning captures day-to-day intraday trend variation
  - Can incorporate explanatory variables (spread, depth)
  - Framework estimates intensity AND joint density
- **Horizon**: Next-trade prediction (seconds), but intraday trend component spans hours
- **Data requirements**: Trade timestamps, optional covariates (spread, depth). We have all of these.
- **Applicability**: **MEDIUM-HIGH**. The intraday trend component (slow-moving) is usable at our horizon. Trade-rate prediction with spread/depth covariates directly implementable. More flexible than ACD.

### D2. Autoregressive conditional duration modelling of HF data
- **ID**: `2111.02300`
- **Authors**: (2021)
- **Methodology**: Modern treatment of ACD models applied to high-frequency data. Extends Engle-Russell (1998) framework.
- **Key findings**:
  - ACD captures duration clustering (fast trades follow fast trades)
  - Conditional duration predicts short-term volatility
  - Extended models handle marks (price changes, volumes)
  - Link: short durations -> high information arrival -> higher volatility
- **Horizon**: Trade-to-trade (seconds), volatility link extends to minutes
- **Data requirements**: Trade timestamps
- **Applicability**: **MEDIUM**. The duration-volatility link is the key actionable insight. Short durations = high information arrival. Can be computed without trade classification.

---

## Section E: Trade Classification and Direction Inference

### E1. Spoofability detection via Hawkes features
- **ID**: `2504.15908v1`
- **Authors**: Fabre, Challet (2025)
- **Methodology**: Multi-scale Hawkes features for order flow. Accounts for size AND posting distance of limit orders. Neural network predicts mid-price movement distribution.
- **Key findings**:
  - **Posting distance** of limit orders is critical for price formation -- models ignoring it are "inadequate"
  - Multi-scale Hawkes features capture both temporal clustering and spatial (price-level) dynamics
  - 31% of large orders could potentially spoof the market (crypto data)
  - Simple neural architecture enables real-time operation
- **Horizon**: Next-event to seconds
- **Data requirements**: Individual limit order events with price levels (L3)
- **Applicability**: **LOW**. Requires L3 data. But the **posting distance** insight is relevant -- our L5 data shows where new depth appears.

### E2. Physics of Price Discovery (Retail Herding)
- **ID**: `2601.11602`
- **Authors**: Kang (2026)
- **Methodology**: Regularized deconvolution + Hawkes analysis of investor flows. Korean equity market 2020-2025.
- **Key findings**:
  - Foreign/institutional flows drive permanent price discovery
  - Individual flows provide contrarian liquidity but panic during herding episodes
  - **Market efficiency is a state variable** conditioned on herding intensity and firm size
  - Institutional impact deteriorates in small-caps during herding
  - Near-explosive self-excitation during individual investor surges
- **Horizon**: Minutes to hours (herding regime detection)
- **Data requirements**: Investor-type flow classification (not available for TAIFEX futures)
- **Applicability**: **LOW** for direct use. But the concept of market efficiency as a state variable (conditioned on intensity regime) is powerful. We can detect intensity surges without investor labels.

### E3. Hawkes-based cryptocurrency forecasting via LOB data
- **ID**: `2312.16190v1`
- **Authors**: Cestari, Barchi, Busetto, Marazzina, Formentin (2023)
- **Methodology**: Hawkes model for LOB events + continuous output error (COE) model for return sign prediction. Non-uniform time sampling.
- **Key findings**:
  - Leveraging non-uniform time structure (event time) beats clock-time approaches
  - Return sign prediction outperforms benchmarks
  - Cumulative profit positive in trading simulation (50 Monte Carlo runs)
  - Applied to Tether/USD on centralized exchange
- **Horizon**: Minutes (return prediction from event patterns)
- **Data requirements**: LOB event timestamps, prices
- **Applicability**: **MEDIUM**. The event-time vs. clock-time insight is relevant. Operating in event time (trade arrivals) rather than clock time could improve our features. The COE model coupling is interesting.

---

## Section F: Synthesis and Actionable Directions

### F1. What can we compute from our data?

| Feature | Computable? | Notes |
|---------|-------------|-------|
| Tick arrival rate (lambda) | YES | Count ticks per window |
| Inter-arrival time statistics | YES | Mean, std, CV of tick gaps |
| Hawkes intensity (univariate) | YES | Fit from tick timestamps |
| Branching ratio n | YES | From Hawkes calibration |
| Intensity bursts | YES | From Christensen-Kolokolov test |
| Hawkes-based volatility | YES | From Lee (2019, 2022) formulas |
| Duration clustering | YES | From ACD-like models |
| Zumbach effect (trend -> activity) | YES | Measure acceleration after moves |
| Signed flow / OFI | PARTIAL | Need tick rule for classification |
| Queue-reactive intensity | YES | L1 depth conditions rates |
| Multi-scale Hawkes features | YES | Multiple exponential kernels |
| Posting distance features | NO | Need L3 data |
| Neural Hawkes / LOBERT | NO | Need L3 data |

### F2. Candidate signals ranked by feasibility and expected horizon

| # | Signal | Horizon | Feasibility | Novel? | Priority |
|---|--------|---------|-------------|--------|----------|
| 1 | **Hawkes branching ratio** (slow regime) | 5-60 min | HIGH | Incremental | **A** |
| 2 | **Tick-rate volatility estimator** | 1-5 min | HIGH | Incremental | **A** |
| 3 | **Intensity burst detection** | Real-time trigger | HIGH | YES | **A** |
| 4 | **Zumbach effect** (trend -> acceleration) | 30s-5 min | MEDIUM | YES | **B** |
| 5 | **Duration CV as information proxy** | 1-5 min | HIGH | YES | **B** |
| 6 | **Queue-conditioned intensity** | 10s-1 min | MEDIUM | Incremental | **C** |
| 7 | **Event-time OFI** (vs clock-time) | 30s-5 min | MEDIUM | Incremental | **C** |
| 8 | **Tick-rule trade classification** | N/A (enhancer) | MEDIUM | NO | **C** |

### F3. Detailed assessment of Priority A candidates

#### (1) Hawkes Branching Ratio as Regime Indicator

**Theory**: The branching ratio n measures what fraction of events are endogenous (triggered by prior events) vs. exogenous (new information). Markets near n=1 are "critical" -- highly endogenous, volatile, prone to cascades. Changes in n over 5-60 min windows reflect genuine regime shifts.

**Implementation**:
- Fit exponential Hawkes to tick timestamps in rolling 5-min windows
- Estimate n = integral of kernel = alpha/beta for exponential kernel
- Feature: `branching_ratio_5min`, `branching_ratio_delta` (change from previous window)
- Expected range: 0.6-0.95 on TXFD6

**Why it might work at 30s+**:
- n evolves slowly (minutes, not seconds) -- inherently a regime indicator
- High n predicts upcoming volatility expansion (papers A1, A6, A7)
- Low n predicts calm/mean-reverting regimes (favorable for CBS)
- Not collinear with existing features (orthogonal to OFI, depth imbalance)

**Risks**:
- Estimation noise in 5-min windows may be large (Hawkes fit is noisy with few hundred events)
- TXFD6 median tick interval = 125ms -> ~2400 ticks per 5 min -> reasonable sample
- Regime shifts in Hawkes parameters can bias branching ratio upward (paper A6 footnote)

**Kill gate**: IC < 0.02 on TXFD6/TMFD6 at 60s horizon, or branching ratio too stable (std < 0.05)

#### (2) Tick-Rate Volatility Estimator

**Theory**: Papers B1 and B2 derive closed-form volatility formulas from Hawkes parameters: sigma^2 proportional to lambda * (1 + Hawkes correction). Simpler version: realized tick count in a window predicts next-window volatility (well-established in ACD literature, paper D2).

**Implementation**:
- Count ticks in 30s windows: `tick_count_30s`
- Compute ratio: `tick_count_ratio = tick_count_30s / tick_count_300s` (acceleration)
- Hawkes volatility: `hawkes_vol_5min` from Lee (2019) formula
- Use as: (a) volatility forecast for risk sizing, (b) regime filter for strategy activation

**Why it might work**:
- tick_count and RV have ~0.7 correlation in literature (papers B1, B2)
- The RATIO (acceleration) is the informative feature, not the level
- 30s tick count is robust (TXFD6: ~240 ticks / 30s, TMFD6: ~100 ticks / 30s)
- Already implicitly used in VRR (RV_5s/RV_300s) from R20 -- this adds tick-count version

**Risks**:
- May be redundant with RV-based features (high correlation)
- Needs detrended IC check (acceleration during trends is contaminated)

**Kill gate**: Incremental IC over existing RV features < 0.01, or correlation with VRR > 0.9

#### (3) Intensity Burst Detection

**Theory**: Christensen & Kolokolov (2024) show that trading activity sometimes exhibits unbounded intensity bursts -- not just high but locally infinite rates. These bursts predict volatility, illiquidity, and price jumps.

**Implementation**:
- Monitor inter-arrival time distribution in sliding window
- Detect when tick rate exceeds 3x rolling median (simple) or use C-K test statistic (rigorous)
- Feature: `intensity_burst_flag`, `burst_magnitude`, `time_since_last_burst`
- Binary signal for strategy gating

**Why it might work**:
- Bursts are rare, discrete events -- not noise
- Empirically linked to: high volatility, illiquidity, drift bursts
- Effects persist for minutes after burst (not just sub-second)
- Directly computable from tick timestamps alone
- Could serve as CBS entry gate (enter AFTER burst subsides)

**Risks**:
- TXFD6 tick rate (8/s median) may not produce true "bursts" as defined in FX
- Need to calibrate threshold for TAIFEX market microstructure
- Too few events per day -> insufficient statistical power

**Kill gate**: < 5 burst events per day on TXFD6, or no significant vol/return differential after bursts

### F4. Papers explicitly requiring trade classification (NOT directly usable)

These papers are important theoretically but require buy/sell labels we lack:
- `2601.23172v2` (signed flow for H_0)
- `2408.03594v1` (buy/sell OFI forecasting)
- `2001.08442v1` (market order sign prediction)
- `2601.11602` (investor-type classification)

**Note on tick rule**: The Lee-Ready tick rule classifies ~75-81% of trades correctly. On TAIFEX futures where we see all trades but no buyer/seller labels, tick rule would give approximate classification. However, R12 already determined we lack the infrastructure for this. Consider as future investment.

---

## Section G: Cross-Reference with Prior Rounds

| Prior finding | This survey confirms/contradicts |
|---------------|----------------------------------|
| R12: Hawkes deferred (no trade classification) | **Partially resolved**: univariate Hawkes does NOT need classification |
| R16: No microstructure alpha on TMFD6 at L1 | **Confirmed for directional signals**. Regime indicators may work differently. |
| R19: HF signals cannot extend to MF via math transforms | **Confirmed**. But regime indicators (branching ratio, bursts) are inherently MF. |
| R20: VRR (RV ratio) as regime filter | **Extended**: tick-rate volatility adds complementary angle |
| R14: CBS benefits from volatility filters | **Strengthened**: Hawkes-based vol and burst detection are natural CBS gates |

---

## Section H: Recommended Next Steps

### Immediate (Priority A -- 4h diagnostic each)

1. **Branching ratio diagnostic**: Fit exponential Hawkes to TXFD6 tick timestamps in 5-min windows. Plot n(t) over 22 days. Compute IC at 30s, 60s, 300s horizons. Check orthogonality with depth_imbalance, OFI.

2. **Tick-rate acceleration diagnostic**: Compute tick_count_30s / tick_count_300s on TXFD6/TMFD6. Correlate with next-window RV. Compare with existing VRR feature. Measure incremental IC.

3. **Intensity burst diagnostic**: Implement simple burst detector (tick rate > 3x 5-min median). Count events per day. Measure vol/return conditional on burst. Test as CBS entry/exit filter.

### Deferred (Priority B -- contingent on A results)

4. **Zumbach effect measurement**: After 40+ bps move (CBS trigger), measure tick-rate acceleration in 5-30s window. Does acceleration predict reversal strength?

5. **Duration CV feature**: Coefficient of variation of inter-arrival times in 60s window. High CV = clustered (informed). Low CV = uniform (noise). Test as standalone feature.

### Infrastructure (Priority C -- longer term)

6. **Tick rule implementation**: Add Lee-Ready trade classification to TickEvent. Enables signed flow features.

7. **Event-time feature engine**: Compute features in event-time (N ticks) rather than clock-time (T seconds). May improve all existing features.

---

## Appendix: Full Paper List

| # | ID | Title (short) | Authors | Year | Relevance |
|---|-----|---------------|---------|------|-----------|
| 1 | 1502.04592v2 | Hawkes in finance (survey) | Bacry+ | 2015 | HIGH |
| 2 | 2601.23172v2 | Unified order flow/impact/vol | Muhle-Karbe+ | 2026 | MEDIUM |
| 3 | 2110.07075v1 | Compound Hawkes mid-price prediction | Sjogren+ | 2021 | MEDIUM |
| 4 | 1712.03106v1 | Compound Hawkes in LOB | Swishchuk+ | 2017 | MEDIUM |
| 5 | 2502.17417v1 | Neural Hawkes LOB sim | Lalor+ | 2025 | LOW |
| 6 | 2005.05730v1 | Quadratic Hawkes (Zumbach) | Fosset+ | 2020 | MEDIUM-HIGH |
| 7 | 1912.00359v2 | Endogenous liquidity crises | Fosset+ | 2019 | MEDIUM |
| 8 | 1901.08938 | Queue-reactive Hawkes | Wu+ | 2019 | MEDIUM |
| 9 | 1907.12025 | Marked Hawkes volatility | Lee+ | 2019 | HIGH |
| 10 | 2207.05939 | Hawkes vol tick structures | Lee | 2022 | HIGH |
| 11 | 1410.3394v1 | Volatility is rough | Gatheral+ | 2014 | MEDIUM |
| 12 | 1609.05177v1 | Rough vol foundations | El Euch+ | 2016 | LOW |
| 13 | 1910.13338v2 | Microscopic to rough vol | Tomas+ | 2019 | LOW |
| 14 | 2408.06519v2 | Unbounded intensity bursts | Christensen+ | 2024 | HIGH |
| 15 | 2504.03445 | Tick-by-tick mean-field | Dai Pra+ | 2026 | LOW |
| 16 | 2303.02038v2 | Self-exciting spread | Ruan+ | 2023 | MEDIUM |
| 17 | 2412.09631v1 | LOB diffusion prediction | Zheng+ | 2024 | LOW |
| 18 | 2001.08442v1 | Marked point process LOB | Muni Toke+ | 2020 | MEDIUM |
| 19 | 2408.03594v1 | Hawkes OFI forecast | Anantha+ | 2024 | MEDIUM |
| 20 | 2507.05749v2 | Event-time anchor | Anantha+ | 2025 | LOW |
| 21 | 2511.12563v1 | LOBERT foundation model | Linna+ | 2025 | LOW |
| 22 | 2504.15908v1 | LOB spoofability Hawkes | Fabre+ | 2025 | LOW |
| 23 | 1403.0998v1 | Semi-parametric duration | Tang+ | 2014 | MEDIUM-HIGH |
| 24 | 2111.02300 | ACD modelling HF | -- | 2021 | MEDIUM |
| 25 | 2601.11602 | Physics of price discovery | Kang | 2026 | LOW |
| 26 | 2312.16190v1 | Hawkes crypto LOB forecast | Cestari+ | 2023 | MEDIUM |

# Stage 1: Microstructure Prediction Literature Survey

**Date**: 2026-03-25
**Scope**: Price impact, queue dynamics, trade arrival processes -- short-horizon (100ms-10s) predictive signals for TWSE/OTC via Shioaji/Fubon
**Constraint**: Shioaji P95 latency ~36-47ms; signal half-life must exceed ~100ms

---

## 1. Literature Landscape Summary

### 1.1 Papers Reviewed (30+ abstracts, 3 full papers)

**Price Impact / Propagator Models**:
- Muhle-Karbe et al. (2026) "A unified theory of order flow, market impact, and volatility" [2601.23172]
- Vodret et al. (2021) "Do fundamentals shape the price response?" [2112.04245]
- Cordoni & Lillo (2022) "Transient impact from Nash equilibrium" [2205.00494]
- Abi Jaber et al. (2025) "Fredholm approach to nonlinear propagator models" [2503.04323]
- Takahashi (2025) "Returns and OFI: Intraday dynamics" [2508.06788] -- **full paper read**

**Queue Dynamics / Queue-Reactive Models**:
- Bodor & Carlier (2024) "A novel approach to queue-reactive models" [2405.18594]
- Sfendourakis (2025) "Multi-dimensional queue-reactive model and signal-driven models" [2506.11843]
- Bodor & Carlier (2025) "Deep learning meets queue-reactive" (MDQR) [2501.08822]

**Hawkes Process / Trade Arrival**:
- Mucciante & Sancetta (2023) "Order book dependent Hawkes process" [2307.09077]
- Anantha & Jain (2024) "Forecasting high-frequency OFI" [2408.03594]
- Jusselin (2020) "Optimal market making with persistent order flow" [2003.05958]
- El Karmi (2025) "Deterministic LOB simulator with Hawkes-driven order flow" [2510.08085]

**Fill Probability / Adverse Selection**:
- Arroyo et al. (2023) "Deep attentive survival analysis in LOBs" [2306.05479]
- Albers et al. (2025) "The market maker's dilemma" [2502.18625]
- Fabre & Ragel (2023) "Interpretable ML for HF execution" [2307.04863]
- Cartea et al. (2023) "Detecting toxic flow" [2312.05827]

**LOB Prediction / Deep Learning**:
- Hu (2026) "Neural HMM with adaptive granularity attention" [2603.20456] -- **full paper read**
- Lee (2024) "Price predictability in LOB" [2409.14157] -- **full paper read**
- Briola et al. (2024) "HLOB: Information persistence and structure in LOBs" [2405.18938]
- Lucchese et al. (2022) "Short-term predictability of returns in order book markets" [2211.13777]
- Fabre & Challet (2025) "Spoofability of LOBs with Hawkes features" [2504.15908]

**OFI / Order Flow Modeling**:
- Hu (2025) "Stochastic price dynamics in response to OFI" (CSI 300) [2505.17388]
- Takahashi (2025) "Returns and OFI: SVAR approach" [2508.06788]
- Cont et al. (2014) -- foundational OFI paper (cited extensively)

---

## 2. Key Insights from Full Paper Reads

### 2.1 Takahashi (2025) -- SVAR-ITH for Returns vs OFI [2508.06788]

**Core finding**: At 1-second frequency in S&P 500 E-mini, price impact (b_r) and flow impact (b_f) are *both* significant and vary intraday. Impulse responses dissipate within ~1 second. Price impact b_r ~ 1/(2D) where D = depth, confirming Cont et al. (2014).

**Key signal idea**: The *time-varying price impact coefficient* b_r(t) itself is a predictive feature. When b_r rises (low depth, pre-announcement), the next OFI has amplified price impact. When b_f drops (traders pull back from price-contingent strategies), directional signals from OFI become more reliable.

**Relevance to us**: We already have OFI features but NOT time-varying impact estimation. A real-time b_r estimator (depth-conditioned OFI sensitivity) would be a novel signal orthogonal to raw OFI.

### 2.2 Lee (2024) -- Price Predictability Decomposition [2409.14157]

**Core finding**: LOB prediction accuracy decomposes into volatility prediction (~69%) and directional prediction (~50% from prices alone, ~71% with volume imbalance). Volume imbalance is THE key directional signal; deeper LOB levels add negligible value.

**Relevance to us**: Confirms our L1 focus is correct. Volume imbalance (which maps to our `l1_imbalance_ppm`) is the primary directional driver. Novel alphas should focus on *conditional* imbalance signals (regime-dependent, impact-weighted) rather than deeper LOB levels.

### 2.3 Hu (2026) -- Neural HMM with Adaptive Granularity [2603.20456]

**Core finding**: Multi-resolution encoding (tick-level via dilated convolutions + minute-level via wavelet-LSTM) with volatility-conditioned gating captures regime shifts better than fixed-resolution models. Hidden Markov states with normalizing flow emissions model complex market regimes.

**Relevance to us**: The key extractable signal idea is *adaptive time-scale weighting* conditioned on local volatility and transaction intensity. This is implementable as a feature that weights short-term vs long-term OFI/imbalance signals based on current volatility regime.

---

## 3. Candidate Alpha Directions

### Candidate A: Hawkes-Conditioned OFI Intensity (Trade Arrival Asymmetry)

**Paper references**:
- Mucciante & Sancetta (2023) [2307.09077] -- LOB-dependent Hawkes intensity
- Anantha & Jain (2024) [2408.03594] -- Hawkes-based OFI forecasting
- Muhle-Karbe et al. (2026) [2601.23172] -- core flow vs reaction flow decomposition

**Signal definition**:
Decompose order flow into "core flow" (exogenous) and "reaction flow" (self-exciting) using a bivariate Hawkes process for buy/sell arrivals:

```
lambda_buy(t) = mu_buy + sum_{t_i < t, side_i = buy} alpha_bb * exp(-beta_bb * (t - t_i))
                      + sum_{t_j < t, side_j = sell} alpha_sb * exp(-beta_sb * (t - t_j))

lambda_sell(t) = mu_sell + sum_{t_i < t, side_i = sell} alpha_ss * exp(-beta_ss * (t - t_i))
                       + sum_{t_j < t, side_j = buy} alpha_bs * exp(-beta_bs * (t - t_j))
```

The predictive signal is the **Hawkes Intensity Imbalance (HII)**:

```
HII(t) = (lambda_buy(t) - lambda_sell(t)) / (lambda_buy(t) + lambda_sell(t))
```

This differs from raw OFI because:
1. It captures *expected future* order flow direction, not just realized flow
2. Self-excitation captures clustering/momentum in arrivals
3. Cross-excitation captures reactive flow (e.g., sell arrivals triggering buy responses)

**Required data inputs**: Tick-level trade data with side classification (we have this from Shioaji tick callbacks)

**Map to existing FeatureEngine**: Requires NEW features -- buy/sell arrival timestamps. Cannot be computed from existing snapshot-based features. Needs event-level tracking.

**Expected predictive horizon**: 500ms - 5s (Hawkes kernels typically have half-lives of 100ms-1s for equity markets; CSI 300 data in Hu (2025) shows OFI memory ~2-5s)

**Novelty vs existing alphas**: Our existing OFI features (`ofi_l1_raw`, `ofi_l1_cum`, `ofi_l1_ema8`) are *backward-looking aggregations*. HII is a *forward-looking intensity prediction* that accounts for self-excitation clustering. It captures the arrival rate asymmetry that precedes price moves, not just the cumulative volume asymmetry.

**Computational complexity**: O(N_events) per update with exponential kernel (each new event updates running sums). Can be computed in <10us with pre-allocated running sums. Kernel parameters estimated offline.

**Implementation complexity**: MEDIUM. Requires:
1. Side classification for each tick event (already available from Shioaji)
2. Running Hawkes intensity estimator (exponential kernel, O(1) per event update)
3. Offline calibration of kernel parameters (alpha, beta, mu) per symbol
4. New FeatureEngine feature slot

---

### Candidate B: Depth-Conditioned Impact Coefficient (Adaptive Price Impact)

**Paper references**:
- Takahashi (2025) [2508.06788] -- time-varying b_r estimation via SVAR-ITH
- Cont et al. (2014) -- OFI impact ~ 1/(2D) theoretical benchmark
- Albers et al. (2025) [2502.18625] -- fill probability vs post-fill returns tradeoff

**Signal definition**:
Estimate a rolling price impact coefficient and use deviations from equilibrium as a predictive signal:

```
# Theoretical equilibrium impact
b_eq(t) = 1 / (2 * depth(t))

# Realized impact (rolling regression OFI -> return)
b_hat(t) = rolling_cov(OFI, return, window=N) / rolling_var(OFI, window=N)

# Impact Surprise Signal
ISS(t) = (b_hat(t) - b_eq(t)) / b_eq(t)
```

When ISS > 0: realized impact exceeds depth-implied impact -- suggests informed trading / adverse selection is elevated. Contra-trend signals from OFI should be downweighted.

When ISS < 0: market is absorbing flow more efficiently than depth suggests -- OFI signals are less informative.

A secondary signal is the **Impact Regime Indicator**:
```
IRI(t) = EMA(|b_hat(t) - b_eq(t)|, span=50)  # Impact volatility
```
High IRI indicates unstable microstructure (regime transition), useful for position sizing.

**Required data inputs**: `mid_price_x2`, `depth_imbalance_ppm` (proxy for depth), `ofi_l1_raw`, returns -- ALL available from existing FeatureEngine.

**Map to existing FeatureEngine**: Fully computable from existing features! Uses `ofi_l1_raw`, `mid_price_x2`, `bid_depth`, `ask_depth`.

**Expected predictive horizon**: 1s - 10s (rolling window of ~50-200 ticks; Takahashi shows b_r varies on 15-minute scale but with micro-level fluctuations)

**Novelty vs existing alphas**: Entirely new dimension. Our alphas use OFI *magnitude* as a signal; this uses OFI *sensitivity* (the partial derivative of price w.r.t. flow) as a signal. It's a second-order feature that modulates the informativeness of all other OFI-based signals.

**Computational complexity**: O(1) per tick with running covariance/variance accumulators. Trivially sub-millisecond.

**Implementation complexity**: LOW.
1. Running covariance of (OFI, return) and variance of OFI -- standard EMA accumulators
2. Depth computation from existing `bid_depth`, `ask_depth`
3. Two new feature slots in FeatureEngine

---

### Candidate C: Queue Depletion Velocity Signal (Fill Probability Gradient)

**Paper references**:
- Arroyo et al. (2023) [2306.05479] -- fill probability modeling via survival analysis
- Albers et al. (2025) [2502.18625] -- queue position vs post-fill returns tradeoff
- Fabre & Ragel (2023) [2307.04863] -- feature importance for fill probability
- Lokin & Yu (2024) [2403.02572] -- state-dependent fill probabilities

**Signal definition**:
Track the rate of queue depletion at the best bid/ask as a directional signal. When one side's queue depletes faster, price is likely to move toward that side:

```
# Queue depletion rate (exponentially weighted)
QDR_bid(t) = EMA(delta_bid_qty(t), span=K)  # negative = depleting
QDR_ask(t) = EMA(delta_ask_qty(t), span=K)  # negative = depleting

# Queue Depletion Imbalance
QDI(t) = (QDR_bid(t) - QDR_ask(t)) / (|QDR_bid(t)| + |QDR_ask(t)| + epsilon)

# Normalized by spread for cross-symbol comparability
QDI_norm(t) = QDI(t) * spread_scaled(t)
```

The insight from Albers et al. is that queue position and depletion speed predict *adverse selection*: fast-depleting queues indicate informed flow arriving at that price level. A market maker should fade the depleting side (contrarian) because fast depletion often precedes reversals.

The **Queue Velocity Divergence** signal adds a second dimension:
```
# Compare short-term vs medium-term depletion
QVD(t) = QDR_bid(t, span=5) / QDR_bid(t, span=50) - QDR_ask(t, span=5) / QDR_ask(t, span=50)
```
This captures *acceleration* in queue depletion, which precedes price moves by 100-500ms.

**Required data inputs**: `l1_bid_qty`, `l1_ask_qty` (changes between ticks), `spread_scaled` -- ALL available from existing FeatureEngine.

**Map to existing FeatureEngine**: Computable from `l1_bid_qty`, `l1_ask_qty` deltas. Partially overlaps with `ofi_l1_raw` but focuses on *rate of change* rather than cumulative flow.

**Expected predictive horizon**: 100ms - 2s (queue depletion precedes price moves at the tick timescale; Arroyo et al. show fill probabilities are predictable at these horizons)

**Novelty vs existing alphas**: Our `ofi_l1_raw` measures net volume flow. QDI measures the *velocity* of queue size changes, which captures a different aspect:
- OFI = "how much flow arrived" (integral)
- QDI = "how fast is the queue shrinking" (derivative)
- QVD = "is the shrinking accelerating" (second derivative)

This is the time-derivative hierarchy of queue dynamics. QDI captures the *urgency* of flow, not just its magnitude. Fast queue depletion with small OFI signals aggressive cancellations (informed trader pulling quotes), which is invisible to OFI.

**Computational complexity**: O(1) per tick. EMA updates on bid/ask quantity changes.

**Implementation complexity**: LOW.
1. Track `delta_bid_qty` = `l1_bid_qty(t) - l1_bid_qty(t-1)` per tick
2. EMA accumulators at two timescales (fast: 5-tick, slow: 50-tick)
3. Two new feature slots in FeatureEngine

---

## 4. Comparative Assessment

| Criterion | A: Hawkes Intensity Imbalance | B: Impact Coefficient | C: Queue Depletion Velocity |
|-----------|------------------------------|----------------------|---------------------------|
| Novelty vs existing | HIGH -- forward-looking intensity | HIGH -- second-order impact | MEDIUM -- derivative of OFI |
| Data requirements | Needs event-level side classification | Existing FeatureEngine | Existing FeatureEngine |
| Computational cost | O(1) per event, ~10us | O(1) per tick, ~1us | O(1) per tick, ~1us |
| Implementation effort | MEDIUM (new event stream) | LOW (EMA accumulators) | LOW (EMA accumulators) |
| Predictive horizon | 500ms - 5s | 1s - 10s | 100ms - 2s |
| Literature support | Strong (Hawkes well-validated) | Strong (Cont/Takahashi) | Moderate (fill prob literature) |
| TWSE applicability | Good (tick-by-tick available) | Good (all features available) | Good (all features available) |
| Latency budget fit | OK (>100ms horizon) | Good (1-10s horizon) | Tight (100ms edge case) |

## 5. Recommendation

**Top 2 for Stage 2 prototyping** (in priority order):

1. **Candidate B: Depth-Conditioned Impact Coefficient** -- Lowest implementation cost, fully computable from existing FeatureEngine, strong theoretical grounding (Cont 2014 + Takahashi 2025), longest predictive horizon (1-10s is well within our latency budget). This is a *modulator* signal that can enhance all existing OFI-based alphas by telling us WHEN OFI is informative.

2. **Candidate C: Queue Depletion Velocity** -- Also low implementation cost, captures a genuinely different signal dimension (rate of change vs level). The 100ms-2s horizon is tighter but still viable. The QVD (acceleration) component is particularly novel and captures informed cancellation patterns invisible to OFI.

3. **Candidate A: Hawkes Intensity Imbalance** -- Highest novelty but requires event-stream infrastructure changes. Recommend deferring to Stage 3 or a separate sprint.

---

## 6. Rejected Directions

| Direction | Why rejected |
|-----------|-------------|
| Deep learning LOB prediction (DeepLOB, HLOB, T-KAN) | Too complex for sub-ms hot path; our Rust kernel can't run neural inference; signals decay too fast for 36ms latency |
| Neural HMM regime detection [2603.20456] | Interesting but regime labels are latent -- hard to validate without extensive backtesting infrastructure |
| Cross-impact / multi-asset propagator [2107.08684] | Requires multi-asset LOB data; TWSE coverage limits applicability |
| Toxic flow detection via PULSE [2312.05827] | Designed for broker/dealer context, not market-making on exchange |
| Full queue-reactive LOB simulation [2405.18594, 2501.08822] | Simulation framework, not a trading signal |

---

## 7. Next Steps (awaiting user selection)

- [ ] User selects 1-2 candidates for Stage 2 prototyping
- [ ] For selected candidates: write Python prototype with FeatureEngine integration
- [ ] Define IC/Sharpe measurement framework against existing alpha baseline
- [ ] Identify TWSE-specific calibration needs (tick size structure, session times)

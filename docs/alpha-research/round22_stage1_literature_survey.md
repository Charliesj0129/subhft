# Round 22 Stage 1: Literature Survey -- LOB Slope, Convexity & Depth Shape

**Date**: 2026-03-28
**Scope**: arXiv survey on order book slope, convexity, depth shape, curvature, and related LOB structural features as predictive signals for short-term price movement.
**Target instruments**: TXFD6 (Taiwan Futures, large), TMFD6 (Mini Taiwan Futures)

---

## 1. Paper Summaries

### 1.1 Foundational Papers

#### P1. Cont, Kukanov & Stoikov (2014) -- "The Price Impact of Order Book Events"
- **ID**: arXiv:1011.6402v3
- **Key methodology**: Study price impact of order book events (limit orders, market orders, cancellations) using NYSE TAQ data for 50 US stocks. Define **Order Flow Imbalance (OFI)** as net supply/demand imbalance at best bid/ask.
- **Main findings**:
  - Linear relation between OFI and price changes, with slope **inversely proportional to market depth**.
  - This is the foundational result: `delta_p = OFI / depth`. The depth acts as a denominator -- thinner books amplify price impact.
  - Robust across time scales and stocks. Implies the "square-root law" of price impact via scaling.
- **Relevance**: The inverse-depth relationship is the theoretical basis for LOB slope signals. When depth is asymmetric across levels, the effective "slope" of the book changes, altering price sensitivity to order flow.

#### P2. Bouchaud, Mezard & Potters (2002) -- "Statistical Properties of Stock Order Books"
- **ID**: cond-mat/0203511v2
- **Key methodology**: Empirical study of order book shape for 3 liquid Paris Bourse stocks. Model the average depth profile as a function of distance from best price.
- **Main findings**:
  - The average order book has a **hump-shaped** profile: depth increases away from the spread, reaches a maximum, then decreases.
  - Incoming limit order prices follow a **power-law** distribution around the current price.
  - A zero-intelligence numerical model quantitatively reproduces the hump shape.
- **Relevance**: Establishes the equilibrium shape of the order book. Deviations from this shape -- particularly asymmetric deviations between bid and ask sides -- are the theoretical basis for slope/convexity signals.

#### P3. Toke (2013) -- "The Order Book as a Queueing System: Average Depth and Influence of Limit Order Size"
- **ID**: arXiv:1311.5661v1
- **Key methodology**: Models the one-sided LOB as a birth-death queueing process with Poisson order/market flows and exponential cancellation lifetimes.
- **Main findings**:
  - Derives an **analytical formula** for the average LOB shape that depends explicitly on order arrival rates, cancellation rates, and order sizes.
  - For a given total incoming volume, **fewer but larger** limit orders produce a deeper book near the spread.
  - Relates book shape to limit order execution probability ("conservation of flow" law).
- **Relevance**: Provides the theoretical machinery connecting order flow parameters to book shape. Deviations from the steady-state shape signal non-equilibrium conditions where informed trading may be occurring.

#### P4. Gould & Bonart (2016) -- "Queue Imbalance as a One-Tick-Ahead Price Predictor"
- **ID**: arXiv:1512.03492v1
- **Key methodology**: Logistic regression between L1 queue imbalance and direction of next mid-price movement for 10 Nasdaq stocks.
- **Main findings**:
  - **Strongly statistically significant** relationship between queue imbalance and next mid-price direction.
  - Large-tick stocks show stronger predictive power than small-tick stocks.
  - Semi-parametric (local logistic regression) slightly outperforms parametric fits.
- **Relevance**: Confirms L1 imbalance as a baseline predictor. The question for LOB slope/convexity research is whether L2-L5 depth information provides **incremental** predictive power beyond L1 imbalance.

### 1.2 Multi-Level Depth & Shape Papers

#### P5. Bechler & Ludkovski (2017) -- "Order Flows and Limit Order Book Resiliency on the Meso-Scale"
- **ID**: arXiv:1708.02715v1
- **Key methodology**: Empirical analysis of LOB depth and shape metrics aggregated via volume-based bucketing on 6 large-tick Nasdaq stocks. Test predictive power of various LOB features using statistical models.
- **Main findings**:
  - **Nonlinear** relationship between trade imbalance and price change.
  - **Deeper LOB shape, rather than just book imbalance, is more relevant** on the meso-scale (volume-bucketed timescale).
  - Limit order flows (addition/cancellation rates) carry the **most predictive power**.
  - The relative rates of order addition vs cancellation at deeper levels are informative.
- **Relevance**: **This is the strongest direct support for LOB slope/shape signals.** The paper explicitly documents that deeper book shape adds predictive value beyond L1 imbalance, particularly on intermediate timescales. This is exactly the timescale relevant for our 36ms+ latency constraint.

#### P6. Elomari-Kessab, Maitrier, Bonart & Bouchaud (2024) -- "Microstructure Modes"
- **ID**: arXiv:2405.10654v1
- **Key methodology**: PCA decomposition of order flow across all LOB levels into "microstructure modes" (symmetric and anti-symmetric). VAR model for mode dynamics on Eurostoxx data over 3+ years.
- **Main findings**:
  - Bid-ask **symmetric** modes (joint depth changes on both sides) carry most predictive power for liquidity.
  - Bid-ask **anti-symmetric** modes (depth shifting from one side to the other) predict price direction.
  - VAR model parameters are **extremely stable in time**.
  - Model becomes **marginally unstable** with more lags (long-memory flows), suggesting endogenous liquidity crisis potential.
  - Relatively high R-squared prediction scores, especially for symmetric liquidity modes.
- **Relevance**: **Highly relevant.** The anti-symmetric mode is essentially a generalized measure of LOB slope/tilt. The symmetric mode captures overall depth changes (convexity/flatness). The stability of the VAR parameters is encouraging for production deployment.

#### P7. Xu et al. (2016) -- "LOB Resiliency After Effective Market Orders"
- **ID**: arXiv:1602.00731v2
- **Key methodology**: Study LOB dynamics (spread, depth, order intensity) surrounding effective market orders of varying aggressiveness on Chinese stocks.
- **Main findings**:
  - Traders submit effective market orders when spreads are low, same-side depth is high, and opposite-side depth is low.
  - **Price resiliency** (mean-reversion) is dominant after aggressive market orders; **price continuation** is dominant after less-aggressive ones.
  - Effective market orders produce **asymmetric stimulus** to limit orders when spread = 1 tick.
  - Spread and depth return to sample average within ~20 best limit updates.
- **Relevance**: Establishes that depth asymmetry predicts the nature of market order flow, and that the resiliency pattern (reversion vs continuation) depends on the aggressiveness of the triggering event. This is relevant for conditional signal construction.

### 1.3 Microprice & Multi-Level Estimators

#### P8. Stoikov (2018) -- "The Micro-Price: A High Frequency Estimator of Future Prices"
- **Referenced in**: Blakely (2024), arXiv:2411.13594v1
- **Key methodology**: Constructs microprice as mid-price + adjustment based on L1 imbalance and spread. Uses Markov chain transition matrices estimated from historical data.
- **Main findings**:
  - Microprice is a better predictor of short-term price moves than mid-price or weighted mid-price.
  - Constructed as the limit of expected future mid-prices conditioned on book state.
- **Relevance**: Baseline L1 estimator. The question is whether deeper-level information improves it.

#### P9. Blakely (2024) -- "High Resolution Microprice Estimates from Limit Orderbook Data"
- **ID**: arXiv:2411.13594v1
- **Key methodology**: Extends Stoikov's microprice with higher price-rank imbalance information. Uses volume percentages at each depth level as features. Encodes via hyperdimensional vectors and Tsetlin machines.
- **Main findings**:
  - Including L2-L5 depth information provides **10-20% improvement** in microprice estimation error.
  - Improvement is **largest during high volatility / wide spread** periods.
  - Blue-chip stocks (tighter spreads) show more consistent improvement than small-caps.
  - Volume percentage distribution across depth levels serves as a compact representation of book shape.
- **Relevance**: **Directly relevant.** Demonstrates that deeper LOB information improves price estimation. The feature construction (volume percentages across levels) is a practical approach to capturing book shape. However, the method uses ML (Tsetlin machines) which may not translate to a simple, interpretable alpha signal.

### 1.4 Cross-Asset LOB Feature Studies

#### P10. Bieganowski & Slepaczuk (2026) -- "Explainable Patterns in Cryptocurrency Microstructure"
- **ID**: arXiv:2602.00776v1
- **Key methodology**: CatBoost + SHAP analysis of LOB features across 5 crypto assets on Binance Futures (1-second frequency, 2022-2025). Features include OFI, spread, depth, VWAP-to-mid deviations.
- **Main findings**:
  - Same feature families dominate across assets (universal microstructure representation).
  - OFI has **monotone effect with concavity at extremes** (diminishing returns for extreme imbalance).
  - **VWAP-to-mid deviations** show asymmetric effects consistent with short-lived pressure and microstructure reversion.
  - Spreads are associated with **diminished predictability** (adverse selection).
  - Tradable under conservative taker execution with reasonable thresholds.
- **Relevance**: The VWAP-to-mid deviation is essentially a weighted measure of depth shape. The concavity finding at OFI extremes is important: it suggests that LOB slope/shape may modulate the OFI signal nonlinearly.

#### P11. Bonart & Lillo (2016) -- "A Continuous and Efficient Fundamental Price on the Discrete Order Book Grid"
- **ID**: arXiv:1608.00756v2
- **Key methodology**: Adapts Madhavan-Richardson-Roomans price formation model to realistic order books with quote discretization and liquidity rebates. Proposes a fundamental price estimator based on **rebate-adjusted volume imbalance** at best quotes.
- **Main findings**:
  - Fundamental price is continuous, efficient, and can sit outside the bid-ask interval.
  - Estimator based on volume imbalance outperforms simpler estimators on 100 Nasdaq stocks.
- **Relevance**: Provides a robust L1-only fundamental price estimator as a baseline against which deeper-level features must demonstrate incremental value.

### 1.5 Price Impact & Book Shape Theory

#### P12. Smith, Farmer, Gillemot & Krishnamurthy (2003) -- "Statistical Theory of the Continuous Double Auction"
- **ID**: cond-mat/0210475v1
- **Key methodology**: Microscopic dynamical statistical model for the double auction under IID random order flow. Dimensional analysis and mean-field approximations.
- **Main findings**:
  - Testable predictions for price volatility, depth vs price, spread, and price impact function.
  - **Highly concave** nature of the price impact function is explained by the model.
  - Order size (granularity) is more significant than tick size in determining market behavior.
- **Relevance**: The concave price impact function means that book depth acts as a **nonlinear buffer**. Thinner regions of the book (steep slope) amplify impact disproportionately. This nonlinearity is the theoretical basis for slope-based signals.

#### P13. Cristelli, Alfi, Pietronero & Zaccaria (2009) -- "Liquidity Crisis, Granularity of the Order Book and Price Fluctuations"
- **ID**: arXiv:0902.4159v2
- **Key methodology**: Microscopic model for LOB dynamics studying how liquidity (average density of stored orders, "granularity g") influences price fluctuations.
- **Main findings**:
  - Price impact depends on both volume and granularity: phi(omega, g) ~ omega^0.59 averaged over g.
  - Dependence on granularity: phi ~ g^(-1), showing **divergence of price fluctuations** as liquidity approaches zero.
  - Even intermediate liquidity levels can produce very large price fluctuations.
- **Relevance**: Quantifies the relationship between book thinness (low granularity = steep slope) and price volatility. A sudden decrease in granularity at specific price levels is a leading indicator of vulnerability.

---

## 2. Candidate Alpha Directions

### Candidate A: Depth-Weighted Slope Asymmetry (DWSA)

**Theoretical basis**: Bechler & Ludkovski (P5) show that deeper LOB shape carries predictive power beyond L1 imbalance on the meso-scale. Cont et al. (P1) show that price impact is inversely proportional to depth. The asymmetry of the depth profile between bid and ask sides -- the "slope" of the book -- should predict the direction and magnitude of the next price move.

**Signal construction**:

```
# Depth-weighted slope at each side (L1-L3 or L1-L5)
bid_slope = sum_{i=1}^{N} (vol_bid[i] * (1/i)) / sum_{i=1}^{N} vol_bid[i]
ask_slope = sum_{i=1}^{N} (vol_ask[i] * (1/i)) / sum_{i=1}^{N} vol_ask[i]

# Higher slope = more volume concentrated near best price (steeper near-touch)
# Signal: asymmetry between bid and ask slope
DWSA = (bid_slope - ask_slope) / (bid_slope + ask_slope)
```

Alternatively, compute the "effective depth slope" as the ratio of L1 volume to total L1-L3 volume on each side:

```
bid_concentration = vol_bid[1] / sum(vol_bid[1:3])
ask_concentration = vol_ask[1] / sum(vol_ask[1:3])
DWSA_v2 = bid_concentration - ask_concentration
```

**Expected horizon and decay**: 5-30 seconds. The signal measures a structural state of the book that should persist for multiple ticks but decay as the book reshapes. Bechler & Ludkovski found the meso-scale (volume-bucketed, roughly 10-60s equivalent) to be where depth shape matters most.

**Data requirements**: L1+L2+L3 minimum. L5 preferred but not essential. Tick-based (event-driven) updates. Available via existing ClickHouse L5 data export.

**Feasibility assessment**:
- Computation: O(1) per tick update with pre-allocated buffers. 3 additions + 2 divisions. Trivially fast.
- Latency: Signal horizon (5-30s) far exceeds 36ms RTT. Safe.
- Cost: At 30s horizon, need IC > 0.030 to exceed ~4 pts RT cost (from R17 breakeven analysis). At 10s, need IC > 0.050.
- Data: L3 is available in ClickHouse. L5 available via `--formats l5` export.

**How it differs from prior work**:
- R11 (mlofi_gradient): Used MLOFI (multi-level OFI) which measures order flow changes across levels. DWSA measures the static depth structure, not flow. Different information source.
- R15 (LOB KE / gravity center): Used "kinetic energy" and gravity center which were physics-inspired transformations. Gravity center IC was -0.025 (reversal). DWSA is a simpler, more interpretable ratio that captures the same intuition but avoids the noise from L3-L5 that killed R15.
- R20 (depth shape with N=20): Was killed for insufficient observations. DWSA uses L1-L3 only (more stable), not L1-L5.

**Key risk**: L1 imbalance (already in FeatureEngine v2 as depth_imbalance) may capture most of the signal, leaving DWSA with negligible incremental IC. This is the same collinearity problem that killed several R15 features (gravity center correlated with depth_imbalance at r=0.70).

---

### Candidate B: Resilience-Conditioned OFI (RC-OFI)

**Theoretical basis**: Xu et al. (P7) show that price resiliency after market orders depends on their aggressiveness. Elomari-Kessab et al. (P6) show that symmetric and anti-symmetric LOB modes capture different dynamics. The key insight: OFI's predictive power should be **conditioned on the book's ability to absorb it** (its resilience). A thin book with high OFI is qualitatively different from a thick book with the same OFI.

**Signal construction**:

```
# Standard OFI (already available in FeatureEngine v2)
OFI = delta_bid_vol[1] - delta_ask_vol[1]

# Book resilience proxy: ratio of depth replenishment rate to depletion rate
# Measured over trailing window (e.g., 30s)
replenishment_rate = count(limit_order_additions_at_L1, window=30s)
depletion_rate = count(trades_at_L1, window=30s)
resilience = replenishment_rate / max(depletion_rate, 1)

# Depth buffer: total depth within 2 ticks of mid
near_depth = sum(vol_bid[1:2]) + sum(vol_ask[1:2])

# Conditioned signal: OFI scaled by inverse depth buffer
RC_OFI = OFI / near_depth  (normalized by near-touch liquidity)
```

The Cont et al. (P1) result directly supports this: `delta_p = OFI / depth`. RC-OFI makes this relationship explicit as a signal rather than a descriptive model.

**Expected horizon and decay**: 1-15 seconds. This is a faster signal because it exploits transient depth imbalances that get arbitraged away quickly. The 36ms RTT is compatible if the signal half-life is >1s.

**Data requirements**: L1+L2. Requires counting order additions and trades at L1, which requires either tick-by-tick event data or trade-level data. We have tick data but NOT trade-level classification (no buy/sell tags). However, near_depth is available from L1+L2 snapshots.

**Feasibility assessment**:
- Computation: O(1). Simple ratio.
- Latency: Marginal. Signal half-life must be verified empirically. If <1s, DOA.
- Cost: At 5s horizon, need IC > 0.100. At 15s, need IC > 0.050. Aggressive targets.
- Data: L1+L2 available. Replenishment rate estimation requires event-level data (tick updates contain this implicitly).

**How it differs from prior work**:
- R11/R16/R18: All tested OFI variants (multi-level, filtered, smoothed) but none conditioned on near-touch depth.
- The denominator (near_depth) is the new component. This is not volume-weighted price (microprice) but rather a sensitivity scaling.
- Cont et al.'s result is well-established but surprisingly we have never explicitly tested `OFI/depth` as a signal.

**Key risk**: May reduce to a noisy version of `OFI * (1/depth)`, which is just a depth-scaled imbalance. If depth is approximately constant across observations (large-tick regime), the scaling adds nothing. TMFD6's median spread = 3 pts suggests it IS a large-tick asset where L1 depth dominates.

---

### Candidate C: Depth Convexity Index (DCI)

**Theoretical basis**: Bouchaud et al. (P2) establish the hump-shaped equilibrium profile of the LOB. Smith et al. (P12) show the price impact function is concave due to this profile. Cristelli et al. (P13) show price fluctuations diverge as granularity approaches zero. The **convexity** of the depth profile (second derivative of cumulative depth vs distance from mid) captures whether the book is "thin near, thick far" (convex = protective buffer) or "thick near, thin far" (concave = vulnerable to slippage).

**Signal construction**:

```
# Cumulative depth at each level
cum_bid = [vol_bid[1], vol_bid[1]+vol_bid[2], vol_bid[1]+vol_bid[2]+vol_bid[3]]
cum_ask = [vol_ask[1], vol_ask[1]+vol_ask[2], vol_ask[1]+vol_ask[2]+vol_ask[3]]

# Second difference (discrete second derivative) = convexity
bid_convexity = cum_bid[2] - 2*cum_bid[1] + cum_bid[0]
# Simplified: = vol_bid[3] - vol_bid[2]
ask_convexity = cum_ask[2] - 2*cum_ask[1] + cum_ask[0]
# Simplified: = vol_ask[3] - vol_ask[2]

# Positive convexity = depth accelerating away from mid (protective)
# Negative convexity = depth decelerating (vulnerable to sweep)

# Signal: differential convexity (bid protection vs ask protection)
DCI = (bid_convexity - ask_convexity) / (abs(bid_convexity) + abs(ask_convexity) + epsilon)
```

Note the simplification: for cumulative depth with linear spacing, the second difference at level i is just `vol[i+1] - vol[i]`. So convexity = whether depth is increasing or decreasing as you move away from the touch.

**Expected horizon and decay**: 10-60 seconds. Convexity is a slower-moving structural feature. It reflects the strategic placement patterns of larger participants who build or withdraw depth at deeper levels over minutes.

**Data requirements**: L3 minimum (for second derivative). L5 preferred for stability. Available in ClickHouse.

**Feasibility assessment**:
- Computation: O(1). Two subtractions per side.
- Latency: Comfortable. 10-60s horizon vs 36ms RTT.
- Cost: At 30s, need IC > 0.030. At 60s, need IC > 0.020. Reasonable targets if signal exists.
- Data: L3 available. L5 available via export.

**How it differs from prior work**:
- R15 tested "LOB kinetic energy" which used spatial momentum (sum of vol * delta_vol). That was velocity-based. DCI is a pure shape feature (static snapshot).
- R15 also tested gravity center (volume-weighted average level), which found IC=-0.025 (reversal) but was killed as too weak. DCI captures a different aspect: not WHERE the volume center is, but whether volume is ACCELERATING or DECELERATING away from the touch.
- R11 (mlofi_gradient) measured the gradient of OFI across levels (flow-based). DCI measures the gradient of depth itself (state-based).
- R20 killed "depth shape" with N=20 observations. DCI needs to be tested with much more data.

**Key risk**: On TXFD6/TMFD6 with 5 levels of depth data, the L3-L5 volumes may be too noisy to extract a reliable second derivative. R15 found that "L3-L5 add noise" for simple features. The convexity signal requires meaningful volume at L2-L3, which may not exist consistently on these contracts.

---

## 3. Kill Criteria

### Candidate A (DWSA -- Depth-Weighted Slope Asymmetry)
| # | Kill criterion | Threshold |
|---|----------------|-----------|
| K1 | Correlation with existing depth_imbalance feature | r > 0.60 (collinear, no incremental value) |
| K2 | Raw detrended IC at 10s horizon | |IC| < 0.015 |
| K3 | Sign consistency across 10-day rolling windows | < 55% of windows same sign |
| K4 | L2-L3 volume existence rate | < 70% of ticks have non-zero L2+L3 volume |
| K5 | IC improvement over L1-only imbalance | < 20% incremental IC |

### Candidate B (RC-OFI -- Resilience-Conditioned OFI)
| # | Kill criterion | Threshold |
|---|----------------|-----------|
| K1 | Signal half-life (autocorrelation decay) | < 1 second |
| K2 | Detrended IC at 5s horizon | |IC| < 0.020 |
| K3 | Correlation between OFI/depth and raw OFI | r > 0.90 (depth scaling adds nothing) |
| K4 | Near-depth coefficient of variation | CV < 0.10 (depth too stable to differentiate) |
| K5 | Net edge after RT costs at optimal horizon | < 0.5 bps |

### Candidate C (DCI -- Depth Convexity Index)
| # | Kill criterion | Threshold |
|---|----------------|-----------|
| K1 | L3 volume existence rate on TXFD6 | < 60% of ticks have meaningful L3 volume |
| K2 | Detrended IC at 30s horizon | |IC| < 0.010 |
| K3 | Convexity stationarity (mean-reverting vs trending) | ADF test p > 0.05 (not stationary = trend contamination) |
| K4 | Correlation with depth_imbalance or DWSA | r > 0.50 (redundant) |
| K5 | L2/L3 volume SNR | vol_std / vol_mean > 3.0 at L3 (too noisy) |

### Universal kill criteria (all candidates)
- **Detrended IC gate** (mandatory per feedback_detrended_ic_gate.md): Raw IC that grows monotonically with horizon indicates trend contamination, not microstructure signal.
- **Cost breakeven**: IC at target horizon must exceed `cost / (2 * vol * horizon_seconds)` implied breakeven.
- **Robustness**: Signal must be consistent in February AND March 2026 data separately (no regime-specific artifact).

---

## 4. Data Infrastructure Needs

### 4.1 Required Data
| Data source | Status | Action needed |
|-------------|--------|---------------|
| L1 tick data (TXFD6, TMFD6) | Available | None |
| L5 depth data in ClickHouse | Available (confirmed R20) | Export via `ch_batch_export.py --formats l5` |
| L3 depth snapshots | Subset of L5 | Extract L1-L3 from L5 export |
| Event-level tick updates | Available (Shioaji callbacks) | Parse from raw tick stream |
| Trade-level buy/sell classification | NOT available | Not needed for Candidates A/C. Candidate B needs near_depth only. |

### 4.2 New FeatureEngine Features (if candidates pass Gate Zero)
| Feature | Formula | Slot | Depends on |
|---------|---------|------|------------|
| `dwsa_l3_x1000` | `1000 * (bid_conc - ask_conc)` where conc = vol[1]/sum(vol[1:3]) | TBD | L3 depth |
| `rc_ofi_x1000` | `1000 * OFI / near_depth` | TBD | L1+L2 depth |
| `depth_convexity_x1000` | `1000 * (bid_cvx - ask_cvx) / (|bid_cvx| + |ask_cvx| + eps)` | TBD | L3 depth |

### 4.3 Gate Zero Diagnostic (Pre-Prototyping)
Before implementing any of these signals, run a quick diagnostic on TXFD6 L5 data:
1. **L2/L3 volume presence**: What fraction of ticks have non-zero volume at L2 and L3?
2. **L2/L3 volume stability**: What is the coefficient of variation of L2/L3 depth?
3. **DWSA vs depth_imbalance correlation**: Compute both on historical data and measure r.
4. **Depth profile shape**: Is TXFD6 hump-shaped (as theory predicts) or monotonic? If monotonic, convexity signal is likely degenerate.

---

## 5. Recommendation & Prioritization

### Priority ordering: A > C > B

1. **Candidate A (DWSA)** -- CONDITIONAL GO for Gate Zero diagnostic.
   - Strongest theoretical backing (Bechler & Ludkovski directly show deeper shape matters on meso-scale).
   - Simplest computation (L1-L3 volume ratios).
   - Main risk is collinearity with existing depth_imbalance.
   - Gate Zero will determine if L2-L3 add anything beyond L1.

2. **Candidate C (DCI)** -- CONDITIONAL GO pending Gate Zero L3 volume check.
   - Theoretically distinct from L1 imbalance (captures acceleration, not level).
   - R15 found L3-L5 adds noise. DCI explicitly tests whether the SECOND derivative (rather than the first derivative / level) is the right transformation.
   - Gate Zero must confirm sufficient L3 volume on TXFD6.

3. **Candidate B (RC-OFI)** -- DEFER pending A/C results.
   - Theoretically clean (Cont et al.'s result) but operationally risky.
   - Half-life concern: if the signal decays sub-second, our 36ms RTT makes it unexecutable.
   - Likely high collinearity with raw OFI on large-tick assets where depth doesn't vary much.
   - Test only if A or C show that depth information has incremental value.

### Key structural finding from the literature

The strongest result across all papers is from Bechler & Ludkovski (2017): **deeper LOB shape matters more than just book imbalance on the meso-scale**. This directly contradicts our R15 finding that "L3-L5 add noise." The resolution may be:

1. **Timescale**: R15 tested at tick-level (microseconds). Bechler & Ludkovski used volume-bucketed data (seconds-minutes). LOB shape may only be predictive at longer horizons where the book has time to react.
2. **Feature construction**: R15 used physics-inspired transforms (kinetic energy, gravity). Simpler ratios (concentration, convexity) may be more robust.
3. **Market structure**: R15 tested on TXFD6 which is a futures contract. Bechler & Ludkovski tested on Nasdaq equities. Futures LOBs may have structurally different depth profiles.

The Gate Zero diagnostic will resolve whether (3) is the binding constraint. If TXFD6 has negligible L2-L3 volume (as suggested by R15's finding that "L1 dominates"), all three candidates are likely DOA and we should redirect to non-LOB-shape alpha sources.

---

## 6. References

1. Cont, R., Kukanov, A., & Stoikov, S. (2014). "The Price Impact of Order Book Events." arXiv:1011.6402v3.
2. Bouchaud, J.-P., Mezard, M., & Potters, M. (2002). "Statistical Properties of Stock Order Books." cond-mat/0203511v2.
3. Toke, I. M. (2013). "The Order Book as a Queueing System." arXiv:1311.5661v1.
4. Gould, M. D. & Bonart, J. (2016). "Queue Imbalance as a One-Tick-Ahead Price Predictor." arXiv:1512.03492v1.
5. Bechler, K. & Ludkovski, M. (2017). "Order Flows and LOB Resiliency on the Meso-Scale." arXiv:1708.02715v1.
6. Elomari-Kessab, S. et al. (2024). "Microstructure Modes." arXiv:2405.10654v1.
7. Xu, H.-C. et al. (2016). "LOB Resiliency After Effective Market Orders." arXiv:1602.00731v2.
8. Stoikov, S. (2018). "The Micro-Price." Quantitative Finance, DOI:10.1080/14697688.2018.1489139.
9. Blakely, C. D. (2024). "High Resolution Microprice Estimates from LOB Data." arXiv:2411.13594v1.
10. Bieganowski, B. & Slepaczuk, R. (2026). "Explainable Patterns in Cryptocurrency Microstructure." arXiv:2602.00776v1.
11. Bonart, J. & Lillo, F. (2016). "A Continuous and Efficient Fundamental Price." arXiv:1608.00756v2.
12. Smith, E. et al. (2003). "Statistical Theory of the Continuous Double Auction." cond-mat/0210475v1.
13. Cristelli, M. et al. (2009). "Liquidity Crisis, Granularity of the Order Book and Price Fluctuations." arXiv:0902.4159v2.
14. Eisler, Z., Bouchaud, J.-P., & Kockelkoren, J. (2012). "The Price Impact of Order Book Events." arXiv:0904.0900v3.
15. Richards, K.-A. et al. (2012). "Heavy-Tailed Features of LOB Volume Profiles in Futures Markets." arXiv:1210.7215v2.

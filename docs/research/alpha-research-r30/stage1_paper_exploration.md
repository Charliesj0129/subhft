# R30 Stage 1: Paper Exploration — Rough Volatility / Fractals / Multi-Scale Memory

**Date**: 2026-04-02
**Researcher**: Agent (Researcher)
**Status**: COMPLETE

---

## 1. Literature Survey Summary

### 1.1 Search Queries Executed

| # | Query | Relevant Hits |
|---|-------|---------------|
| 1 | "rough volatility" "Hurst exponent" microstructure | 4 (rate-limited, partial) |
| 2 | "fractional Brownian motion" "limit order book" | 0 relevant (search noise) |
| 3 | "multifractal" "high frequency" trading | 3 relevant |
| 4 | "rough Hurst" "realized volatility" forecasting | 2 relevant |
| 5 | "multi-scale" memory "price process" alpha signal | 0 relevant (search noise) |
| 6 | "rough volatility" trading signal strategy | 2 relevant |
| 7 | "rough volatility" Hurst microstructure Hawkes | 12 highly relevant |
| 8 | "multifractal" "detrended fluctuation" volatility | 4 relevant |
| 9 | Gatheral Jaisson Rosenbaum "volatility is rough" | 3 foundational |
| 10 | "Hurst exponent" regime volatility trading | 3 relevant |
| 11 | Web: RFSV forecasting trading 2024-2025 | 5 relevant |
| 12 | Web: MFDFA trading strategy futures | 3 relevant |
| 13 | Web: Zumbach effect quadratic Hawkes 2024 | 4 relevant |

### 1.2 Key Papers Identified

#### Foundational (Rough Volatility Theory)

| Paper | arXiv ID | Core Contribution |
|-------|----------|-------------------|
| **Gatheral, Jaisson, Rosenbaum (2014)** "Volatility is rough" | `1410.3394` | Seminal paper: log-vol ~ fBm with H ~ 0.1. RFSV model outperforms HAR for RV forecasting. |
| **El Euch, Fukasawa, Rosenbaum (2016)** "Microstructural foundations of leverage effect and rough volatility" | `1609.05177` | Hawkes-based microscopic model generates rough Heston in the limit. Links HFT microstructure to rough vol. |
| **Jaisson, Rosenbaum (2013)** "Limit theorems for nearly unstable Hawkes processes" | `1310.2033` | Nearly unstable Hawkes -> CIR/Heston in limit. Branching ratio near 1 = high endogeneity. |
| **Jaisson, Rosenbaum (2015)** "Rough fractional diffusions as scaling limits..." | `1504.03100` | Heavy-tailed Hawkes kernel -> fractional CIR with H = alpha - 1/2. Agent-based foundation for rough vol. |

#### Rough Volatility Extensions

| Paper | arXiv ID | Core Contribution |
|-------|----------|-------------------|
| **Dandapani, Jusselin, Rosenbaum (2019)** "From quadratic Hawkes to super-Heston rough vol with Zumbach effect" | `1907.06151` | Quadratic Hawkes captures Zumbach effect (past trends -> future vol). Super-Heston limiting model. |
| **Chudasama, Iyer (2025)** "Asymmetric super-Heston-rough vol with Zumbach effect" | `2508.16566` | Bivariate QHawkes with buying/selling asymmetry. TRA preserved in limit. |
| **Tomas, Rosenbaum (2019)** "From microscopic price dynamics to multidimensional rough vol" | `1910.13338` | Cross-asset rough vol from Hawkes. Momentum and mean-reversion roles identified. |
| **Mouti (2023)** "Rough volatility: evidence from range volatility estimators" | `2312.01426` | Confirms H < 0.1 using range-based estimators (not HF returns). RFSV beats AR/HAR/GARCH. |

#### Volatility Forecasting with fBm/Hurst

| Paper | arXiv ID | Core Contribution |
|-------|----------|-------------------|
| **Bibinger, Yu, Zhang (2025)** "Modeling and Forecasting RV with Multivariate fBm" | `2504.15985` | mfBm with different Hurst exponents per asset. Outperforms vector HAR. |
| **Chong, Hoffmann, Liu, Rosenbaum, Szymanski (2022)** "Statistical inference for rough vol: CLTs" | `2210.01216` | Optimal H estimator from HF prices. Semiparametric, no a priori vol relationship needed. |
| **Bibinger (2019)** "Cusum tests for changes in Hurst exponent and volatility of fBm" | `1904.04556` | Change-point detection for H and vol. Pivotal test (no parameter knowledge needed). |

#### Multifractal / Multi-Scale

| Paper | arXiv ID | Core Contribution |
|-------|----------|-------------------|
| **Brandi, Di Matteo (2022)** "Multiscaling and rough volatility" | `2201.10466` | Tests interplay between price multiscaling and vol roughness. rBergomi model. Real data shows **opposite** relationship vs model. |
| **Kantelhardt et al. (2002)** "Multifractal DFA of nonstationary time series" | `physics/0202070` | Foundational MFDFA method. Distinguishes multifractality from correlations vs fat tails. |
| **Barunik, Kristoufek (2012)** "On Hurst exponent estimation under heavy-tailed distributions" | `1201.4786` | GHE estimator most robust under heavy tails. Time-dependent H on S&P500 1983-2009. |

#### Regime / Trading Applications

| Paper | arXiv ID | Core Contribution |
|-------|----------|-------------------|
| **Prakash, James, Menzies, Francis (2020)** "Structural clustering of volatility regimes for dynamic trading strategies" | `2004.09963` | Unsupervised regime clustering via change-point detection + distance matrix. Dynamic trading strategy validated. |
| **Baldovin et al. (2012)** "Ensemble properties of HF data and intraday trading rules" | `1202.2447` | Scaling properties of intraday returns -> martingale model -> trading strategy exploiting linear correlations. |
| **Jiang, Chen, Zhou (2008)** "DFA of intertrade durations" | `0806.2444` | Multifractal nature of intertrade durations on Shenzhen exchange. Long memory confirmed. |

---

## 2. Candidate Alpha Directions

### Candidate A: RFSV-Based Realized Volatility Forecasting for Vol-Timing

**Paper References**: `1410.3394`, `2312.01426`, `2504.15985`, `2210.01216`

**Core Alpha Mechanism**:
The RFSV (Rough Fractional Stochastic Volatility) model of Gatheral et al. treats log-realized-volatility as a fractional Brownian motion with Hurst exponent H ~ 0.1. This yields a simple, parsimonious forecasting formula for future realized volatility that depends on essentially one parameter (H). The key insight is that volatility has a very specific memory structure: recent observations carry disproportionate weight (due to H << 0.5), creating a forecasting edge over models like HAR/GARCH that assume smoother volatility dynamics.

The alpha is NOT a directional price signal. Instead, it is a **volatility-timing** strategy:
1. Estimate H from rolling windows of realized volatility (using range-based or HF estimators).
2. Forecast next-period realized volatility via RFSV formula.
3. When RFSV forecasts a vol expansion, enter vol-buying positions (long straddle / long gamma). When RFSV forecasts vol contraction, sell vol (short straddle / short gamma).
4. On TMFD6/TXFD6 specifically: use vol forecast to dynamically size directional positions -- larger in low-vol regimes (tighter spreads, better fill quality), smaller in high-vol regimes (wider spreads, more noise).

**Signal Type**: Volatility forecasting -> position sizing / vol-timing
**Expected Holding Period**: 1 hour to 1 day (vol forecasts meaningful at these scales)
**Data Requirements**: L1 tick data for RV computation (we have this). Range-based estimators possible from OHLC.

**Why It Might Work Where R6-R29 Failed**:
- R6-R29 tried to predict price direction at ultra-short horizons, competing with broker RTT. This candidate predicts volatility at medium horizons (hours to days), where the signal half-life is vastly longer than broker RTT.
- The RFSV model is empirically validated across asset classes with H consistently ~ 0.1. This is a stylized fact, not a fragile microstructure edge.
- The strategy does not require sub-tick execution. Volatility forecasts evolve slowly enough for 30-40ms RTT to be irrelevant.
- Can be implemented as vol-timing overlay on existing strategies or as standalone option/straddle strategy on TXO.

**Key Risks**:
- Cost of vol trading: Buying/selling vol via options (TXO) has its own bid-ask spread. If vol forecast edge is small, costs dominate.
- TMFD6-specific: TMFD6 is a single futures contract, not an option. Vol-timing must translate to position sizing, not direct vol trading. This limits the alpha extraction mechanism.
- H estimation instability: H can be sensitive to window length and microstructure noise.
- Not truly novel: HAR model already captures multi-scale vol memory. The marginal improvement from RFSV over HAR may be small in practice.

**Kill Conditions**:
- RFSV vol forecast does not beat HAR-RV on TMFD6/TXFD6 out-of-sample (measured by QLIKE/MSE).
- Vol-timing position sizing does not improve Sharpe ratio vs flat sizing by > 0.3.
- H estimate on TMFD6 is unstable (coefficient of variation > 0.5 across rolling windows).

**Feasibility Score: 3/5**

---

### Candidate B: Zumbach Effect / Volatility Feedback as Directional Signal

**Paper References**: `1907.06151`, `2508.16566`, `1609.05177`, `1310.2033`

**Core Alpha Mechanism**:
The Zumbach effect (also called time-reversal asymmetry, TRA) is the empirical observation that past trends in returns predict future volatility more strongly than past volatility predicts future squared returns. Concretely: large recent directional moves (either up or down) predict elevated future volatility, but the direction matters asymmetrically -- down moves predict more vol increase than up moves (leverage effect).

The alpha exploits the conditional asymmetry of the Zumbach effect:
1. Compute a rolling "trend signal" = recent cumulative signed returns over multiple windows (5s, 30s, 300s).
2. Compute a "Zumbach statistic" = sum of (signed return_i * signed return_j) for i < j in recent history. This captures the quadratic feedback term.
3. When the Zumbach statistic is large and positive (strong recent trend), predict vol expansion. When near zero (choppy), predict vol contraction.
4. Directional component: After a strong down trend with elevated Zumbach statistic, the leverage effect predicts further vol increase, creating a conditional mean-reversion opportunity (price overshot + vol spike = reversion). After a strong up trend, vol increase is weaker, suggesting trend continuation is more likely.

**Signal Type**: Directional (conditional on volatility-feedback asymmetry) + volatility forecasting
**Expected Holding Period**: 1 minute to 30 minutes (faster than Candidate A, but still >> RTT)
**Data Requirements**: L1 tick data (price returns). Already computed in FeatureEngine v3 (OFI, spread, imbalance at 5s/30s/300s windows).

**Why It Might Work Where R6-R29 Failed**:
- R6-R29 used linear microstructure features (OFI, spread, imbalance). The Zumbach effect is inherently quadratic -- it depends on products of past returns, which are orthogonal to linear features. This is a genuinely new feature class.
- The holding period (1-30 min) is much longer than the sub-second horizons that failed in R6-R29. Cost-per-trade is amortized over larger moves.
- The leverage effect asymmetry (down moves -> more vol -> mean reversion) provides conditional directional information that was not tested in previous rounds.
- Compatible with existing FeatureEngine infrastructure -- the Zumbach statistic is a simple rolling computation.

**Key Risks**:
- Signal strength on TMFD6: The Zumbach effect is documented primarily on liquid US equities and major indices. TMFD6 (Mini-TAIEX) may have different characteristics.
- Quadratic features are noisy: Products of returns amplify noise. The SNR may be too low at intraday timescale.
- Cost barrier: Even at 1-30 min holding periods, the 3.92 pts round-trip on TMFD6 requires the conditional signal to deliver > 4 pts per trade.
- Overfitting risk: Quadratic features have many degrees of freedom. Must use strict OOS validation.

**Kill Conditions**:
- Zumbach statistic shows no time-reversal asymmetry on TMFD6 data (test via TRA statistic).
- Conditional mean-reversion after down-trend + high Zumbach does not yield > 4 pts expected move.
- Quadratic feature IC < 0.02 (detrended) on 1-30 min horizon.

**Feasibility Score: 2.5/5**

---

### Candidate C: Rolling Hurst Regime Classifier for Adaptive Strategy Selection

**Paper References**: `1201.4786`, `2004.09963`, `1904.04556`, `2201.10466`, `physics/0202070`

**Core Alpha Mechanism**:
The Hurst exponent H of a price series characterizes its memory structure: H > 0.5 = trending/persistent, H = 0.5 = random walk, H < 0.5 = mean-reverting/anti-persistent. The key insight is that H is time-varying and transitions between regimes are detectable.

The alpha is a meta-strategy / regime classifier:
1. Estimate rolling Hurst exponent on TMFD6/TXFD6 using GHE method (most robust under heavy tails).
2. Classify: H > 0.55 = trending, 0.45 < H < 0.55 = random walk, H < 0.45 = mean-reverting.
3. Route to appropriate sub-strategy per regime.
4. Use Bibinger (2019) cusum test for H change-points.

**Signal Type**: Regime classification -> strategy selection
**Expected Holding Period**: 30 minutes to several hours
**Data Requirements**: L1 tick data for price returns.

**Why It Might Work Where R6-R29 Failed**:
- R6-R29 applied a single strategy across all market conditions. The core failure may be that signals are regime-dependent and average to zero across regimes.
- The Hurst regime classifier doesn't generate trades itself but decides which sub-strategy to activate.

**Key Risks**:
- Sub-strategy performance: If no sub-strategy is profitable on TMFD6 (as R6-R29 suggest), the regime classifier doesn't help.
- H estimation lag and TMFD6 may be persistently H ~ 0.5 with no meaningful regime variation.

**Kill Conditions**:
- Rolling H on TMFD6 shows coefficient of variation < 0.1 (no regime variation).
- Regime-conditioned returns show no statistical difference.
- Sub-strategies fail Gate C independently.

**Feasibility Score: 2/5**

---

## 3. Cross-Candidate Comparison

| Criterion | A: RFSV Vol-Timing | B: Zumbach Feedback | C: Hurst Regime |
|-----------|-------------------|--------------------|-----------------| 
| Signal novelty vs R6-R29 | Medium (vol forecast) | High (quadratic features) | Medium (regime layer) |
| Holding period | 1h - 1d | 1min - 30min | 30min - hours |
| Cost sensitivity | Low (fewer trades) | High (more trades) | Depends on sub-strategy |
| RTT sensitivity | None | Low-Medium | None |
| Standalone viability | Medium (needs options or sizing overlay) | Medium (needs sufficient edge) | Low (needs sub-strategies) |
| Implementation complexity | Low-Medium | Medium | Medium |
| Theory strength | Very strong (universal H~0.1) | Strong (Zumbach documented) | Moderate |
| TMFD6 applicability risk | Medium | High | High |
| **Feasibility Score** | **3/5** | **2.5/5** | **2/5** |

---

## 4. Recommendation

**Primary**: Candidate A (RFSV Vol-Timing) -- safest and most theoretically grounded. The shift from directional price prediction to volatility forecasting is the key strategic pivot from R6-R29.

**Secondary**: Candidate B (Zumbach Feedback) -- higher risk/reward. Quadratic features are genuinely untested in R6-R29.

**Not standalone**: Candidate C (Hurst Regime) -- best as enhancement to A or B.

---

## 5. Honest Assessment

1. Rough volatility is a well-established stylized fact (H ~ 0.1 is universal). Theory is rock-solid.
2. The gap between description and alpha is wide. Most literature is about modeling/pricing, not trading signals on single futures.
3. The core R6-R29 lesson still applies: any alpha on TMFD6 must overcome 3.92 pts cost.
4. The strongest application would be TXO options market-making (vol forecasting sets prices), not TMFD6 directional trading.
5. Candidate B (Zumbach) is the most promising for pure TMFD6 futures but carries highest uncertainty.

---

## References

- Gatheral, Jaisson, Rosenbaum (2014). "Volatility is rough." arXiv:1410.3394
- El Euch, Fukasawa, Rosenbaum (2016). "Microstructural foundations of leverage effect and rough volatility." arXiv:1609.05177
- Jaisson, Rosenbaum (2013). "Limit theorems for nearly unstable Hawkes processes." arXiv:1310.2033
- Jaisson, Rosenbaum (2015). "Rough fractional diffusions as scaling limits." arXiv:1504.03100
- Dandapani, Jusselin, Rosenbaum (2019). "Quadratic Hawkes to super-Heston with Zumbach effect." arXiv:1907.06151
- Chudasama, Iyer (2025). "Asymmetric super-Heston-rough vol with Zumbach effect." arXiv:2508.16566
- Tomas, Rosenbaum (2019). "Microscopic price dynamics to multidimensional rough vol." arXiv:1910.13338
- Mouti (2023). "Rough volatility: evidence from range volatility estimators." arXiv:2312.01426
- Bibinger, Yu, Zhang (2025). "Modeling and Forecasting RV with Multivariate fBm." arXiv:2504.15985
- Chong et al. (2022). "Statistical inference for rough volatility: CLTs." arXiv:2210.01216
- Brandi, Di Matteo (2022). "Multiscaling and rough volatility." arXiv:2201.10466
- Kantelhardt et al. (2002). "Multifractal DFA." arXiv:physics/0202070
- Barunik, Kristoufek (2012). "Hurst exponent estimation under heavy tails." arXiv:1201.4786
- Bibinger (2019). "Cusum tests for Hurst exponent changes." arXiv:1904.04556
- Prakash et al. (2020). "Structural clustering of volatility regimes." arXiv:2004.09963
- Baldovin et al. (2012). "Ensemble properties of HF data." arXiv:1202.2447

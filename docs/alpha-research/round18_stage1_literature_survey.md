# Round 18 Stage 1: Literature Survey -- 1min+ Holding Period Strategies for TMFD6

**Date**: 2026-03-26
**Researcher**: Claude (Researcher Agent)
**Status**: COMPLETE -- 3 candidates proposed for Challenger/Execution review

---

## Executive Summary

After surveying 60+ arXiv papers and cross-referencing with R12-R17 cumulative findings, we propose 3 candidate alpha directions that:
- Target holding periods of 1 minute to overnight
- Do NOT depend on sub-second execution, queue priority, or maker rebates
- Are implementable with available TMFD6 L1 data (9.16M rows, 58 days)
- Have cost breakeven thresholds compatible with TMFD6's 1.33 bps RT cost

All three are deliberately chosen to be **orthogonal** to the exhausted L1 microstructure alpha space (OFI, depth imbalance, spread regime, entropy -- all tested and killed in R13-R17).

---

## Candidate A: Trend-Scaled Momentum with Cubic Reversion (TSM-CR)

### Paper References
1. **Safari & Schmidhuber (2025)** -- "Trends and Reversion in Financial Markets on Time Scales from Minutes to Decades" [arXiv:2501.16772v2]
   - 14 years of futures tick data + 30 years daily futures across 24 assets (equity indices, rates, FX, commodities)
   - Key finding: Markets are in a **trending regime** on timescales from ~1 hour to several years, and a **reversion regime** on shorter/longer scales
   - Weak trends persist; strong trends revert before reaching statistical significance
2. **Schmidhuber (2020)** -- "Trends, Reversion, and Critical Phenomena in Financial Markets" [arXiv:2006.07847v4]
   - Establishes the cubic trend-reversion model: E(r) = a + b*phi + c*phi^3
   - Trend persistence coefficient `b` positive from hours to years, peaks at 3-12 months
   - Reversion coefficient `c` is negative and universal across asset classes
   - Both confirmed via bootstrapping and out-of-sample testing over 30 years
3. **Singha, Aguilera-Toste & Lahiri (2025)** -- "Forecast-to-Fill: Benchmark-Neutral Alpha in Gold Futures" [arXiv:2511.08571v1]
   - Practical implementation: smoothed trend-momentum regime signal + volatility targeting + ATR exits
   - Sharpe 2.88 OOS on gold futures, net of 0.7 bps cost
   - Key insight: forecast-to-fill engineering (signal -> sizing -> execution) transforms modest predictability into tradable alpha

### Signal Description
The core signal is a **trend strength t-statistic** (phi) computed over multiple lookback windows (1h, 4h, 1d), combined with the empirically universal cubic reversion model. Rather than pure trend-following (which R17 showed is unstable on TMFD6 push-response), this explicitly models when trends are ABOUT TO REVERT.

The signal decomposes into two components:
1. **Weak-trend momentum**: When |phi| < critical threshold (~1.5-2.0 sigma), trends persist -> go WITH the trend
2. **Strong-trend reversion**: When |phi| > critical threshold, trend is about to revert -> fade the trend

This is fundamentally different from the L1 microstructure signals tested in R13-R17, because it operates on price returns over multi-hour windows, not tick-by-tick order flow.

### Proposed Implementation on TMFD6
1. Compute rolling trend t-statistic phi(t) using EMA returns over 1h, 4h windows
2. Fit cubic model: predicted_return = b * phi + c * phi^3 (calibrate b, c from rolling 20-day window)
3. Position sizing: volatility-targeted (e.g., 15% annualized vol target), Kelly-fraction adjusted
4. Entry: market order when predicted_return > cost threshold (1.33 bps)
5. Exit: ATR-based trailing stop OR time-based (hold until opposite signal)
6. Session filter: apply CBS-style ToD gating (exclude opening 15min, closing 5min)

### Expected Horizon
- **Primary**: 1-4 hour holding period
- **Trade frequency**: 2-5 trades per session (day + night combined)
- **Entry**: aggressive (market order) since signal operates on hour-scale, 36ms RTT irrelevant

### Feasibility Assessment
- **IC expectation**: Safari & Schmidhuber report R^2 ~ 0.5-2% for cubic model on daily data aggregated across assets. For single-asset intraday, expect IC ~ 0.03-0.08 at 1-4h horizon.
- **Cost breakeven**: At 4h horizon, IC breakeven = 0.043 (our target: 0.03-0.08). Marginal but plausible.
- **Data requirement**: 58 days is TIGHT for calibrating b, c. Need rolling 20-day window, leaving only ~38 OOS days. Statistical significance will be borderline.
- **Key advantage**: Signal is completely orthogonal to all R13-R17 microstructure signals. Uses ONLY price returns, no LOB features.

### Risk Factors
1. **Regime instability**: The `b` coefficient has been shrinking over decades (markets becoming efficient). On TMFD6 specifically, unclear if enough trend-following capital exists to create persistent trends.
2. **Sample size**: 58 days is marginal for fitting a cubic model. Risk of overfitting.
3. **TMFD6 specificity**: Universal results are aggregated across 24 assets. TMFD6 mini-TAIEX may not conform to universal scaling if dominated by retail flow.
4. **Night session**: The trend regime structure may differ between day (08:45-13:45) and night (15:00-05:00) sessions. Need to validate separately.

---

## Candidate B: HMM Regime-Conditioned Momentum (HMM-RCM)

### Paper References
1. **Christensen, Godsill & Turner (2020)** -- "Hidden Markov Models Applied To Intraday Momentum Trading With Side Information" [arXiv:2006.08307v1]
   - HMM with 2-3 latent states for intraday momentum
   - Key innovation: no time-lag (unlike moving average crossovers), accurate regime shifts at market change points
   - Side information: realized volatility ratio + intraday seasonality improve prediction
   - Bayesian inference via forward algorithm for t+1 return prediction
2. **Bucci & Ciciretti (2021)** -- "Market Regime Detection via Realized Covariances" [arXiv:2104.03667v1]
   - VLSTAR model outperforms unsupervised clustering for regime detection
   - Regime switches can be used as trading filters
3. **Blake, Gandhi & Jakkula (2025)** -- "Improving S&P 500 Volatility Forecasting through Regime-Switching Methods" [arXiv:2510.03236v1]
   - Regime-switching HAR model for realized volatility forecasting
   - Coefficient-based soft-regime clustering outperforms all baselines during all time periods

### Signal Description
A 2-state (or 3-state) Hidden Markov Model where:
- **State 1 (Trending)**: Positive drift, moderate volatility -> momentum signal
- **State 2 (Reverting/Choppy)**: Near-zero drift, high volatility -> no-trade / mean-reversion signal
- (Optional State 3: High-volatility breakout)

The HMM infers the current regime probabilistically at each bar. The key advantage over naive momentum (EMA crossover, MACD) is that the HMM does NOT lag at turning points -- it can instantaneously switch regime probability when new data arrives.

Side information channels:
- **Realized volatility ratio**: RV(5min) / RV(30min) -- high ratio = recent vol spike (regime change likely)
- **Intraday seasonality**: TMFD6 has known patterns (R14/R17: opening=momentum, rest=mean-reversion)
- **Volume profile**: normalized volume vs. session average

### Proposed Implementation on TMFD6
1. Aggregate TMFD6 ticks into 5-minute OHLCV bars
2. Compute bar returns + realized vol features
3. Fit 2-state Gaussian HMM on rolling 30-day window via Baum-Welch
4. At each new bar, compute filtered state probabilities P(trending|data)
5. Trading rule:
   - P(trending) > 0.7 AND return_sign == direction -> enter WITH momentum (market order)
   - P(trending) < 0.3 -> enter AGAINST last 30min return (mean-reversion)
   - Otherwise: flat
6. Position sizing: Kelly-fraction * vol-target
7. Hold: until regime probability flips OR time stop (max 2 hours)

### Expected Horizon
- **Primary**: 5 minutes to 2 hours
- **Trade frequency**: 3-8 trades per session
- **Bar frequency**: 5-minute bars (aggregate from tick data)

### Feasibility Assessment
- **IC expectation**: Christensen et al. report profitable strategies on equity index futures. For a 2-state HMM on 5min bars, expect IC ~ 0.04-0.10 at 30min horizon.
- **Cost breakeven**: At 30min horizon, IC breakeven = 0.043. At 1h, 0.030. Comfortably within range if HMM calibrates well.
- **Data requirement**: 58 days of 5min bars = ~16,700 bars. Sufficient for 2-state HMM (needs ~500-1000 bars for stable calibration).
- **Key advantage**: Adaptively identifies when momentum vs. reversion is active. Directly addresses the R14 finding that "opening=momentum, rest=mean-reversion" by learning this pattern automatically.

### Risk Factors
1. **Overfitting**: HMM with too many states or features can overfit on 58 days. Must limit to 2 states, minimal side information.
2. **Regime label instability**: HMM states can swap labels between calibration windows. Need to anchor states by drift sign.
3. **Forward-looking bias**: Must use strictly online (filtered, not smoothed) probabilities. Viterbi (smoothed) would introduce look-ahead.
4. **Computational cost**: Baum-Welch recalibration on 30-day rolling window every day is feasible. Pre-compute offline, deploy frozen parameters intraday.
5. **Non-stationarity**: HMM assumes stationary emission distributions within each state. TMFD6 volatility varies significantly (night session lower vol than day). May need separate day/night models.

---

## Candidate C: Volatility-Regime Breakout Strategy (VRB)

### Paper References
1. **Rosenzweig (2026)** -- "Fast Times, Slow Times: Timescale Separation in Financial Timeseries Data" [arXiv:2601.11201v1]
   - Method for separating fast/slow processes in financial time series
   - Applications to mean reversion and tail risk management
   - Generalized eigenvalue problem framework for variance/tail stationarity
2. **Blake, Gandhi & Jakkula (2025)** -- "Improving S&P 500 Volatility Forecasting through Regime-Switching Methods" [arXiv:2510.03236v1]
   - Regime-switching HAR model for realized volatility forecasting
   - RV forecasting R^2 typically 30-60% (far more predictable than returns)
3. **Leung & Zhou (2021)** -- "Optimal Dynamic Futures Portfolios Under a Multiscale Central Tendency OU Model" [arXiv:2102.12601v1]
   - Multiscale OU model for futures, closed-form optimal trading strategies
   - Applicable when price reverts to a slowly-moving central tendency

### Signal Description
This strategy exploits the well-documented relationship between **realized volatility regime** and **subsequent directional moves**. The key insight: volatility is MUCH more predictable than returns (R^2 ~ 30-60% for RV forecasting vs. <2% for return forecasting). We convert volatility predictability into directional alpha via:

1. **Volatility compression detection**: When RV drops below its 20-day rolling percentile P20, market is in a "quiet" regime. Quiet regimes precede breakouts.
2. **Breakout direction**: Use the sign of the slow-moving trend (e.g., 4h EMA slope) to predict breakout direction.
3. **Entry trigger**: When RV_5min / RV_1h > 2.0 (volatility expansion from compressed state), enter in direction of the emerging move.
4. **Exit**: ATR trailing stop (2x ATR) or time-based max hold (4 hours).

This is a **volatility breakout** strategy, not a return-prediction strategy. It's well-suited for TMFD6 because:
- TMFD6 has known volatility seasonality (high at open, compressed mid-session, elevated at close)
- R14's CBS already proved that large moves ARE predictable on TMFD6 (contrarian after 40 bps)
- VRB is the COMPLEMENTARY signal: it catches the INITIAL move, while CBS catches the REVERSION

### Proposed Implementation on TMFD6
1. Compute rolling 1h and 4h realized volatility from 1-minute returns
2. Compute RV percentile rank over 20-day rolling window
3. When RV_1h < P20 (vol compression):
   - Arm the breakout trigger
   - Track 4h EMA slope for breakout direction
4. When RV_5min / RV_1h > 2.0 AND armed:
   - Enter in direction of 4h EMA slope (market order)
   - Set stop-loss at 2x ATR(1h)
   - Set time-stop at 4 hours
5. Exit when:
   - Trailing stop hit (2x ATR)
   - Time stop (4h max hold)
   - Opposite breakout signal fires

### Expected Horizon
- **Primary**: 30 minutes to 4 hours
- **Trade frequency**: 1-3 trades per session (breakouts are infrequent events)
- **Entry latency tolerance**: Very high. 36ms RTT irrelevant for hour-scale holds.

### Feasibility Assessment
- **IC expectation**: Volatility compression -> breakout is well-established. Expected IC ~ 0.05-0.12 at 1-4h horizon, but CONDITIONAL on vol compression trigger (selective entry).
- **Cost breakeven**: At 1h+ horizon with 1.33 bps cost, IC breakeven = 0.030-0.043. Well within range.
- **Data requirement**: 58 days adequate for RV percentile calibration. Simple enough that overfitting risk is low.
- **Key advantage**: Very simple signal (no ML, no parameter-heavy model). Naturally selective (only trades during vol compression -> expansion transitions). Complementary to CBS.
- **Synergy with CBS**: VRB catches the initial directional breakout. CBS catches the mean-reversion after overshoot. Together they form a "breakout then fade" cycle.

### Risk Factors
1. **False breakouts**: Vol expansion can be a spike-and-fade without sustained directional move. ATR stop is the main defense.
2. **Directional ambiguity**: 4h EMA slope may be flat during vol compression. Need minimum slope threshold.
3. **Sample size for breakouts**: With 58 days, and 1-3 breakouts/day, get ~60-180 signals. Borderline for statistical significance.
4. **Night session**: Vol compression is more common in night session (lower activity). Night breakouts may have different characteristics.
5. **Overlap with CBS**: Both strategies are active during high-volatility moves. Need careful position management to avoid doubling up.

---

## Candidate Comparison Matrix

| Dimension | A: TSM-CR | B: HMM-RCM | C: VRB |
|---|---|---|---|
| **Complexity** | Medium (cubic fit) | High (HMM calibration) | Low (RV percentiles) |
| **Holding period** | 1-4 hours | 5min - 2 hours | 30min - 4 hours |
| **Trades/session** | 2-5 | 3-8 | 1-3 |
| **IC expectation** | 0.03-0.08 | 0.04-0.10 | 0.05-0.12 |
| **Overfitting risk** | Medium (cubic model) | High (HMM states) | Low (simple rules) |
| **Data sufficiency** | Marginal (58 days) | Adequate | Adequate |
| **CBS complementarity** | Moderate | High | Very high |
| **Implementation effort** | Low-Medium | Medium-High | Low |
| **Novelty** | High (cubic reversion) | Medium (well-known) | Low (classic pattern) |

## Recommendation for Stage 2

**Priority order**: C (VRB) > A (TSM-CR) > B (HMM-RCM)

Rationale:
1. **VRB** is simplest, lowest overfitting risk, naturally complementary to CBS, and most likely to survive the 58-day data constraint. Should be prototyped FIRST.
2. **TSM-CR** is intellectually the most novel (cubic reversion model has strong cross-asset empirical support) but needs careful calibration on limited data.
3. **HMM-RCM** has the highest theoretical ceiling but also the highest overfitting risk and implementation complexity. Consider as stretch goal.

## Exhaustion List (DO NOT RE-PROPOSE)

For Challenger reference, these directions were considered and explicitly rejected:
- L1 microstructure (OFI, depth, spread, entropy) -- R13-R17 EXHAUSTED
- Bidirectional MM -- R13 structurally unprofitable at 36ms RTT
- Options volume imbalance (OIDS) -- TXO data is 99.7% quotes, no trade ticks
- Push-response (Vlasiuk) -- R16 momentum in March, unstable
- MLOFI gradient -- Gate C FAIL (fees > returns)
- TSMC 2330 lead-lag standalone -- IC=0.061, p=0.066, dead
- Entropy magnitude (Singha 2025) -- tested 3 times, always fails
- Sentiment/NLP-based -- no data pipeline, out of scope
- Cross-asset lead-lag (beyond 2330) -- no additional data sources available
- RL/deep learning strategies -- insufficient data for training (58 days)
- Spread regime strategies -- R16 proved spread = contract maturity artifact, not signal

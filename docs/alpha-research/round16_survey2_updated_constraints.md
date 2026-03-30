# Round 16 Stage 1B: Second Literature Survey (Updated Constraints)

**Date**: 2026-03-26
**Context**: First survey's candidates all failed on L1 imbalance signal weakness. This survey excludes proven-dead directions and accounts for empirical findings.

---

## Updated Constraint Set

### Proven Dead (exclude from search)
- L1 depth imbalance as directional predictor (IC=0.04, accuracy 52%)
- Spread-conditional maker (adverse selection trap: median PnL -8 pts when spread >= 6)
- Lead-lag between instruments (MXFD6 cost 20x > signal, Round 14)
- Continuous bidirectional market making at 36ms RTT (Round 13)
- Any strategy requiring >60% accuracy from single L1 features

### What We Know Works
- **Execution timing via imbalance**: 2.6-9.4 pts improvement per trade in wide-spread regime (Jan/Feb TMFD6), 0.4-1.2 pts in tight-spread (March)
- **Passive limit orders**: Save 1.2-1.4 pts/trade vs taker (universal)
- **Regime dependence**: Signal strength varies 10-20x between spread regimes on TMFD6

### Instrument and Cost
- Target: TMFD6 (Mini-TAIEX futures), XMT cost = 40 NTD RT = 4.0 pts = 1.33 bps
- Data: 9.16M rows (58 days), L5 depth, tick-by-tick
- Median spread: 3 pts (March) to 34 pts (Jan/Feb)
- Tick rate: 1.8/sec

---

## Search Methodology (Survey 2)

Extended search beyond q-fin.TR to include q-fin.ST, q-fin.CP, q-fin.MF. Focused on:
1. Push-response / mean-reversion patterns (not directional prediction)
2. Spread regime dynamics and prediction
3. Multi-feature LOB prediction (beyond raw imbalance)
4. Execution optimization beyond simple passive
5. VPIN/PIN and toxicity metrics

---

## Candidate Direction A: Push-Response Anomalies (Conditional Mean Reversion)

### Source Paper
- Vlasiuk & Smirnov (2025). "Push-response anomalies in high-frequency S&P 500 price series." arXiv:2511.06177v1. [q-fin.TR, q-fin.CP, q-fin.ST]

### Core Idea
Using 1,500 days of SPY NBBO event-time data, the authors form ordered pairs of "push" (backward price increment) and "response" (forward price increment). They find:
1. For short lags (1-5,000 ticks), responses cluster near zero -- high short-term efficiency
2. Beyond that range, **large pushes increasingly correlate with non-zero responses**
3. **Large NEGATIVE pushes produce STRONGER positive responses than equally large positive pushes** -- asymmetric liquidity replenishment after sell-side shocks
4. The anomaly is "invisible in unconditional returns" -- only visible in conditional analysis
5. It "can be used to define tradable pockets and risk controls"

### Why It Might Work for TMFD6
1. **Not directional prediction**: This is conditional mean-reversion after large moves, not continuous direction forecasting. Large moves are identifiable in real-time.
2. **Asymmetric reversion**: Sell-side shocks revert more strongly than buy-side. This is a structural feature of order books (asymmetric liquidity replenishment).
3. **Works on SPY (most efficient market)**: If push-response anomalies exist in SPY, they likely exist in less efficient TMFD6. SPY has narrower spreads and faster traders -- TMFD6's slower pace and wider spreads may amplify the effect.
4. **Data available**: We have 58 days of TMFD6 tick data to compute push-response maps.
5. **Implementation**: After a large price drop (push), post a passive buy limit. Wait for reversion. Exit on mean reversion or timeout.
6. **Compatible with wide-spread regime**: Large pushes often coincide with wide spreads. In the Jan/Feb regime where our execution timing already works, push-response would add directionality to the timing.

### Key Risks
- SPY at ~1500 ticks lag-range is MUCH higher frequency than TMFD6 at 1.8 ticks/sec. The lag range may not transfer.
- "Tradable pockets" may be too rare for meaningful trading frequency on TMFD6.
- The asymmetric reversion may be too small relative to 4.0 pts cost.
- TMFD6's lower liquidity means large pushes may be genuine information, not temporary pressure.

### Data Requirements
- TMFD6 tick data (HAVE: 9.16M rows)
- Need: push-response map computation (computationally intensive but feasible)

### Estimated Signal Half-Life
Seconds to minutes (dependent on push size; larger pushes = longer reversion)

### Implementation Complexity
LOW-MEDIUM. Core analysis is statistical (no ML required). Strategy logic: detect large push, post contrarian limit, exit on reversion or timeout.

### Quick Validation
Compute push-response map on TMFD6 data. Measure conditional expected response for pushes > 2 sigma at various lag ranges (10, 50, 100, 500 ticks).

---

## Candidate Direction B: Multi-Feature LOB Prediction with CatBoost

### Source Papers
- Bieganowski & Slepaczuk (2026). "Explainable Patterns in Cryptocurrency Microstructure." arXiv:2602.00776v1. [q-fin.TR, q-fin.CP, q-fin.ST]
- Berti & Kasneci (2025). "TLOB: A Novel Transformer Model with Dual Attention for Price Trend Prediction." arXiv:2502.15757v3. [q-fin.ST, q-fin.TR]

### Core Idea
Bieganowski & Slepaczuk document that **the same engineered LOB features predict returns across assets** with remarkably similar importance rankings. Using CatBoost with 1-second frequency on crypto (BTC, LTC, ETC, ENJ, ROSE), they achieve profitable taker strategies with top-of-book features. Key features in order of importance:
1. Order flow imbalance (OFI) -- but computed from TRADE data, not just L1 snapshots
2. Spread features
3. Depth imbalance at multiple levels
4. Trade intensity features
5. Return momentum features

Their finding: "feature rankings and partial effects are stable across assets despite heterogeneous liquidity and volatility."

TLOB extends this with transformer-based dual attention on the standard FI-2010 LOB benchmark, outperforming all prior methods at all horizons.

### Why It Might Work for TMFD6
1. **Multi-feature approach**: Our L1 imbalance alone is too crude. A proper feature engineering pipeline using L5 depth, OFI, spread dynamics, trade intensity, and return autocovariance could produce much stronger signals.
2. **We have L5 data**: TMFD6 ClickHouse data includes 5-level bid/ask arrays. We can compute L1-L5 depth features.
3. **Cross-asset portability**: If features transfer across BTC/LTC/ETC, they might transfer to TMFD6.
4. **CatBoost is simple to implement**: No deep learning infrastructure needed. Gradient boosting on engineered features.
5. **Explainability via SHAP**: Can validate that model predictions are based on economically meaningful features, not overfitting.

### Key Risks
- **Crypto != TAIFEX**: Different market microstructure (continuous vs session-based, maker rebates vs none, retail vs institutional).
- **1-second frequency may be too coarse**: TMFD6 at 1.8 ticks/sec means only ~2 ticks per 1-second bar. Sub-bar information is lost.
- **Still need to overcome 4.0 pts cost**: Even with better predictions, the move-to-cost ratio is the binding constraint.
- **Overfitting risk**: CatBoost on 58 days of data with many features is an overfitting minefield.
- **TLOB's performance "deteriorates when trends are defined using average spread"** -- the authors acknowledge that translation to profitable strategies is the hard part.

### Data Requirements
- TMFD6 L5 tick data from ClickHouse (HAVE: 9.16M rows)
- Feature engineering pipeline (BUILD)
- Train/test split: need strict walk-forward validation on 58 days

### Estimated Signal Half-Life
100ms-5s (sub-second to multi-second, depending on feature horizon)

### Implementation Complexity
MEDIUM-HIGH. Requires: (1) feature engineering pipeline (~20 features from L5 data), (2) CatBoost training with walk-forward validation, (3) careful backtest with realistic fills and costs.

### Quick Validation
Build top-5 features (OFI, depth imbalance, spread, trade intensity, return momentum), train CatBoost on first 40 days, test on last 18. Measure directional accuracy and IC at 1s/5s/10s horizons.

---

## Candidate Direction C: Spread Regime Prediction for Execution Optimization

### Source Papers
- He, Shirvani, Shao, Rachev, Fabozzi (2024). "Beyond the Bid-Ask: Strategic Insights into Spread Prediction." arXiv:2404.11722v2. [q-fin.TR, q-fin.RM]
- Farzulla (2026). "The Extremity Premium: Sentiment Regimes and Adverse Selection." arXiv:2602.07018v2. [q-fin.ST]

### Core Idea
Rather than predicting price direction, predict WHEN the spread will be wide or narrow. On TMFD6, the spread regime determines everything:
- Wide spread (Jan/Feb regime): IC 0.19, execution timing saves 6.9 pts
- Tight spread (March regime): IC 0.007, no timing value

If we can predict transitions between spread regimes, we can:
1. Activate execution timing during wide-spread periods
2. Switch to pure passive execution during tight-spread periods
3. Avoid maker fills during wide-but-toxic periods (MC-8 showed adverse selection during spread >= 6 in March, but NOT in Jan/Feb)

### Why It Might Work for TMFD6
1. **Not directional prediction**: We predict spread regime, not price direction. This is a fundamentally different and possibly easier prediction problem.
2. **Exploits our strongest finding**: The 10-20x signal strength difference between spread regimes is the largest effect we've measured. Capitalizing on regime identification is high-value.
3. **Spread prediction is well-studied**: ARMA-GARCH and similar models capture spread dynamics well (He et al. 2024).
4. **Binary output**: Wide/narrow is a simpler classification than up/down/flat. Easier to achieve high accuracy.
5. **Practical implementation**: "Use execution timing when spread is wide, don't when it's narrow" is a simple rule that requires only regime detection.

### Key Risks
- **The Jan/Feb vs March difference may be structural, not predictable**: If the regime change is due to contract rollover or market-wide volatility, it's a known calendar effect, not a microstructure prediction.
- **Within-day spread variation may be too noisy**: The signal works across multi-day regimes. Within a single day, spread variation may not be predictable enough.
- **Execution timing value of 0.4-1.2 pts (tight regime) may not cover implementation cost**.

### Data Requirements
- TMFD6 spread time series (HAVE)
- Volatility indicators (HAVE: can compute from mid-price)
- Session time features (HAVE)

### Estimated Signal Half-Life
Hours to days (regime-level, not tick-level)

### Implementation Complexity
LOW. Spread is directly observable. Regime detection can be as simple as rolling average spread threshold. No ML required for initial version.

### Quick Validation
Compute rolling 5-minute spread on TMFD6. Test: does past-5-minute spread predict next-5-minute spread? If autocorrelation > 0.8, regime is persistent and detectable.

---

## Papers Reviewed but Rejected (Survey 2)

| Paper | arXiv ID | Reason |
|-------|----------|--------|
| TLOB: Transformer for LOB Price Prediction | 2502.15757 | Interesting architecture but: (a) "performance deteriorates when trends defined using spread" -- authors admit cost-adjusted performance is poor, (b) requires deep learning infra we lack |
| When AI Trading Agents Compete | 2510.27334 | RL market maker exploiting meta-orders -- we are the exploited party (retail), not the exploiter |
| Multiple DeFi/AMM papers | Various | Automated market makers on Ethereum are structurally different from centralized LOB |
| Multimodal Stock Price Prediction (Russian) | 2503.08696 | Requires news/NLP data we don't have for TWSE |
| From Deep Learning to LLMs in Quant | 2503.21422 | Survey paper, no specific actionable strategy |
| Fast Times, Slow Times (Timescale Separation) | 2601.11201 | Interesting method but applied to daily frequencies, not intraday |

---

## Ranking and Recommendation

### 1. Candidate C: Spread Regime Prediction (HIGHEST PRIORITY)
- **Why first**: Capitalizes on our strongest empirical finding (10-20x regime effect), lowest implementation complexity, and is immediately testable. Even a simple rolling-spread threshold would capture most of the value.
- **Quick win**: Check spread autocorrelation. If high (likely), implement as "activate execution timing when 5-min avg spread > X" rule.

### 2. Candidate A: Push-Response Anomalies
- **Why second**: Novel direction not explored in Rounds 12-16. Conditional mean-reversion after large moves is fundamentally different from directional prediction. Computationally simple to validate.
- **Quick test**: Compute push-response map on TMFD6 tick data. Check if large pushes produce measurable conditional responses.

### 3. Candidate B: Multi-Feature CatBoost
- **Why third**: Highest potential ceiling (proper ML on 20+ features) but highest implementation and overfitting risk. Should only pursue if A and C yield positive results and we want to improve further.
- **Dependency**: Requires feature engineering pipeline that also benefits A and C.

### Recommended Validation Sequence
1. **Day 1**: Compute spread autocorrelation (Candidate C quick kill/pass)
2. **Day 1**: Compute push-response map on TMFD6 (Candidate A quick kill/pass)
3. **Day 2-3**: If either passes, build targeted prototype
4. **Day 3-5**: If both pass, engineer feature pipeline (benefits B as well)

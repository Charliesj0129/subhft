# Round 16 Stage 1: Literature Survey (q-fin.TR, 2024-2026)

**Date**: 2026-03-25
**Researcher**: Alpha Research Agent
**Scope**: arXiv q-fin.TR papers (Jan 2024 - Mar 2026), focused on strategies viable under TWSE/TAIFEX retail constraints

---

## Executive Summary

Surveyed 60+ papers across q-fin.TR and related categories. Three candidate directions emerged that could generate alpha despite our constraints (36ms Shioaji RTT, no maker rebates, 2.0 bps sell tax). All three exploit structural microstructure phenomena with signal half-lives in the seconds-to-minutes range, well above our latency floor.

**Key insight from the literature**: The most promising recent work converges on a single theme -- **the fill probability vs. post-fill returns tradeoff is THE fundamental constraint for maker strategies** (Albers et al. 2025). Profitable maker trading requires counter-trading the prevailing imbalance when a private signal predicts a reversal. This aligns perfectly with our Round 15 finding that depth asymmetry predicts REVERSAL on TXFD6.

---

## Search Methodology

### Queries Executed (9 searches)
1. `"order flow" AND ("toxic" OR "adverse selection" OR "information asymmetry")` -- q-fin.TR, 2024+
2. `"optimal execution" OR "VWAP" OR "TWAP"` -- q-fin.TR, 2024+
3. `"regime" AND ("market microstructure" OR "limit order book")` -- q-fin.TR, 2024+
4. `"inventory management" OR "market making" OR "spread"` -- q-fin.TR, 2024+ (by date)
5. `"order flow imbalance" OR "price impact" OR "trade classification"` -- q-fin.TR, 2024+
6. `"queue reactive" OR "queue position" OR "queue priority"` -- q-fin.TR, 2024+
7. `"cross-asset" OR "lead-lag" OR "futures"` -- q-fin.TR, 2024+
8. `"volatility" OR "realized variance" OR "intraday"` -- q-fin.TR, 2024+
9. `"Hawkes" OR "self-exciting" AND "trading"` -- q-fin.TR, 2024+

### Papers Downloaded and Read in Full
- **2502.18625v2** - "The Market Maker's Dilemma" (Albers et al.) -- READ IN FULL
- **2503.18005v1** - "A Simple Strategy to Deal with Toxic Flow" (Cartea & Sanchez-Betancourt) -- READ IN FULL
- **2603.20456v1** - "Neural HMM with Adaptive Granularity Attention" (Hu) -- READ IN FULL

### Papers Reviewed via Abstract and Key Sections
- **2505.17388v1** - "Stochastic Price Dynamics in Response to OFI" (Hu & Zhang)
- **2408.03594v1** - "Forecasting High Frequency OFI" (Anantha & Jain)
- **2602.00776v1** - "Explainable Patterns in Cryptocurrency Microstructure" (Bieganowski & Slepaczuk)
- **2511.15262v1** - "RL in Queue-Reactive Models" (Espana et al.)
- **2501.08822v1** - "Deep Learning Meets Queue-Reactive" (Bodor & Carlier)
- **2506.11843v1** - "Multi-dimensional queue-reactive and signal-driven models" (Sfendourakis)
- **2407.04510v1** - "Unwinding Toxic Flow with Partial Information" (Barzykin et al.)
- **2502.04027v1** - "HF Market Manipulation Detection with Markov-modulated Hawkes" (Fabre & Toke)
- **2512.12924v1** - "Interpretable Hypothesis-Driven Trading" (Deep et al.)
- **2511.07434v1** - "RL-Exec: Impact-Aware RL for Optimal Liquidation" (Duflot & Robineau)
- **2506.11813v1** - "Optimal Execution under Liquidity Uncertainty" (Chevalier et al.)
- **2512.15732v1** - "The Red Queen's Trap: Limits of Deep Evolution in HFT" (Chen)

---

## Candidate Direction #1 (RECOMMENDED): Imbalance Reversal Detection for Selective Maker Entries

### Source Papers
- **Primary**: Albers, Cucuringu, Howison, Shestopaloff (2025). "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off." arXiv:2502.18625v2. [q-fin.TR]
- **Supporting**: Sfendourakis (2025). "Multi-dimensional queue-reactive model and signal-driven models." arXiv:2506.11843v1. [q-fin.TR]

### Core Idea
The paper establishes empirically (live Binance BTC perpetual, 232,897 real orders) that fill probability and post-fill returns are **negatively correlated** for maker orders. Orders with high fill probability (posted on the thin side of the book) suffer adverse selection; orders with favorable imbalance (posted on the thick side) rarely fill. The authors identify "reversals" -- cases where the order book imbalance falsely predicts the next price change -- as the key to profitable maker trading. A contrarian maker order posted during a reversal achieves both high fill probability AND positive post-fill returns.

Their reversal prediction model uses:
- Momentum features: return autocovariance over 5s windows (negative = oscillation, favors reversal), 100ms sharp price drops (positive = isolated event, favors reversal)
- Activity features: shorter average time between trades and shorter top-of-book survival times favor reversals
- Book features: large pre-existing bid liquidity penalizes reversal probability
- Volatility: elevated short-term volatility slightly raises reversal probability

### Why It Might Work for Us
1. **Signal half-life: seconds to tens of seconds** -- well above our 36ms RTT. The reversal signal builds over 5-15 second windows.
2. **Exploits our existing infrastructure**: We already have 18 LOB features in FeatureEngine v2, including depth_imbalance, spread, and OFI variants. Adding reversal detection features (return autocovariance, top-of-book survival time, recent price drop indicators) is incremental.
3. **Compatible with Round 15 finding**: Our established result that depth asymmetry predicts REVERSAL on TXFD6 (IC=-0.025) is exactly the foundational observation this strategy builds on. The literature now provides a principled framework to monetize it.
4. **Selective entry**: Not continuous MM -- we only enter when a reversal is predicted. This avoids the structural MM unprofitability at 36ms RTT (Round 13).
5. **Maker fees + no taker fee needed**: On TAIFEX, maker orders avoid crossing the spread. Even without maker rebates, avoiding taker fees is significant.
6. **The "Unprofitability Principle"**: The paper honestly states profits are "likely not scalable" on BTC/Binance. But TXFD6 is far less efficient than BTC perpetual -- the bar for reversal detection may be lower.

### Key Risks / Why It Might Fail
- **Queue position on TXFD6**: At 36ms RTT, we may not achieve front-of-queue positions. The paper shows that back-of-queue maker orders are unprofitable even in reversals.
- **TXFD6 tick structure**: If TXFD6 spread is typically 1 tick, the reversal mechanics should transfer. If spread is wider, the dynamics change.
- **2.0 bps sell tax**: Even for a profitable reversal, the sell tax eats into margins. Need net edge > 2 bps per round-trip.
- **Reversal frequency**: On TXFD6, imbalance predicts next price change ~55-60% of the time (our data). Reversals are the ~40-45% minority. The signal must be selective enough to avoid the diagonal cases.
- **Training data requirements**: Need sufficient TXFD6 tick data with L1+ queue depth to train reversal classifier.

### Data Requirements
- TXFD6 L1 tick data with bid/ask queue sizes (HAVE)
- L5 depth data (HAVE)
- Order-level data for queue position modeling (PARTIALLY HAVE via Shioaji callbacks)
- Minimum 4-6 weeks of training data at tick resolution

### Estimated Signal Half-Life
5-30 seconds (reversal events resolve within this window)

### Implementation Complexity
MEDIUM -- Builds on existing FeatureEngine v2 features. Requires: (1) return autocovariance feature, (2) top-of-book survival time tracker, (3) reversal classifier (logistic regression sufficient per paper), (4) selective order submission logic.

---

## Candidate Direction #2: OFI Regime-Conditional Signal with Horizon Selection

### Source Papers
- **Primary**: Hu & Zhang (2025). "Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures." arXiv:2505.17388v1. [q-fin.MF, q-fin.TR]
- **Supporting**: Anantha & Jain (2024). "Forecasting High Frequency Order Flow Imbalance." arXiv:2408.03594v1. [q-fin.TR]

### Core Idea
Hu & Zhang model OFI impact as an Ornstein-Uhlenbeck process with memory and mean-reverting characteristics. Three key findings:
1. OFI acts as a market "shock" with memory and mean-reversion (not a permanent impact)
2. **Horizon-dependent heterogeneity**: conventional metrics (Sharpe-like "response ratios") interact differently with OFI at different forecast horizons. The optimal trading horizon is NOT universal.
3. **Regime-dependent dynamics**: OFI forecasting power and memory decay vary across market regimes. Some regimes amplify OFI signal, others suppress it.

The Anantha & Jain paper complements this by showing that Hawkes processes with Sum-of-Exponential kernels provide the best OFI forecasts on the National Stock Exchange (India), capturing lagged dependence between bid and offer order flow.

### Why It Might Work for Us
1. **CSI 300 Index Futures are structurally similar to TXFD6**: Both are index futures on Asian exchanges with similar market microstructure (centralized LOB, tick-by-tick data).
2. **We already compute OFI**: FeatureEngine v2 has `ofi_l1_scaled` and `mlofi_gradient_x1000`. This direction adds regime-conditioning and optimal horizon selection on top of existing signals.
3. **Addresses the Round 14 finding differently**: Round 14 found directional signal ceiling of ~0.001 bps UNCONDITIONALLY. But regime-conditional OFI may unlock pockets where the signal is 10-100x stronger, trading only in those pockets.
4. **OU mean-reversion model**: The mean-reverting OFI impact model directly supports a reversal/contrarian approach, consistent with our Round 15 depth-reversal finding.
5. **Signal half-life: 1-60 seconds** depending on regime, well above 36ms.

### Key Risks / Why It Might Fail
- **The 0.001 bps ceiling may be regime-invariant on TXFD6**: If the directional signal is uniformly weak across all regimes, conditioning will not help.
- **Regime identification latency**: If regimes shift faster than we can detect them, we trade in the wrong regime.
- **CSI 300 != TXFD6**: Different liquidity, different participant mix, different tick structure. Results may not transfer.
- **Hawkes calibration**: Requires careful calibration of the self-exciting process parameters. Misspecification degrades the OFI forecast.
- **Transaction costs**: Even if regime-conditional OFI is predictive, the edge must exceed 2 bps sell tax + commission.

### Data Requirements
- TXFD6 L1 tick data with bid/ask sizes (HAVE)
- Sufficient history to identify regimes (>= 4 weeks, HAVE)
- Volatility regime indicators (can derive from existing data)

### Estimated Signal Half-Life
1-60 seconds (regime-dependent; strongest in high-activity regimes)

### Implementation Complexity
MEDIUM-HIGH -- Requires: (1) OFI memory/decay parameter estimation (OU calibration), (2) regime detection module (HMM or volatility-threshold based), (3) horizon-adaptive entry/exit logic, (4) Hawkes process calibration for OFI forecasting (optional enhancement).

---

## Candidate Direction #3: Toxic Flow Detection for Adverse Selection Avoidance

### Source Papers
- **Primary**: Cartea & Sanchez-Betancourt (2025). "A Simple Strategy to Deal with Toxic Flow." arXiv:2503.18005v1. [q-fin.TR]
- **Supporting**: Barzykin, Boyce, Neuman (2024). "Unwinding Toxic Flow with Partial Information." arXiv:2407.04510v1. [q-fin.TR, q-fin.MF]
- **Supporting**: Fabre & Toke (2025). "High-Frequency Market Manipulation Detection with Markov-modulated Hawkes." arXiv:2502.04027v1. [q-fin.TR]

### Core Idea
Cartea & Sanchez-Betancourt derive the optimal broker strategy for dealing with informed ("toxic") vs. uninformed order flow in closed form. The key result is that the optimal strategy is a **linear combination of four observable state variables**: own inventory, informed trader inventory, informed volume, and uninformed volume. Critically, they provide an **algorithm that bypasses individual parameter calibration** -- the strategy coefficients can be learned directly from historical data via a simple optimization (Algorithm 1 in the paper).

The Barzykin et al. paper extends this to partial information (unobserved toxicity), showing the PnL gap between partial and full information is only ~0.01% -- meaning you do not need to perfectly classify toxic flow to benefit.

### Why It Might Work for Us
1. **Defensive, not directional**: This is not about predicting price direction (which we have shown is near-impossible on TXFD6). Instead, it is about **avoiding adverse selection** when we do trade. It improves the quality of our existing signals.
2. **Directly applicable as a filter**: Even if our primary alpha is weak, filtering out toxic-flow periods can dramatically improve Sharpe by avoiding the worst trades.
3. **Simple implementation**: The optimal strategy is LINEAR in state variables. No complex ML model needed. The algorithm learns coefficients from data without parameter calibration.
4. **Robust to partial information**: Barzykin et al. show that imperfect toxicity classification still captures most of the benefit (0.01% gap).
5. **Composable with Candidates #1 and #2**: This works as a defensive overlay. Combine reversal detection (Candidate #1) with toxic flow filtering (Candidate #3) for highest quality entries.
6. **Signal half-life: seconds to minutes** -- toxicity is a regime, not a tick-by-tick signal.

### Key Risks / Why It Might Fail
- **Broker-centric model**: The paper assumes the agent receives order flow from clients. We are a retail trader receiving fills from the exchange. The translation to our context requires adaptation -- we classify incoming taker flow as informed/uninformed based on markout analysis, not client identity.
- **Client classification on TXFD6**: We cannot directly observe who is an "informed trader" vs. "uninformed." Must infer from order flow patterns (large vs. small orders, time of day, correlation with subsequent price moves).
- **May reduce already-low trading frequency**: If the filter is too aggressive, it blocks most entries, leaving insufficient trading volume for the strategy to be meaningful.
- **The 2.0 bps tax still applies**: Avoiding bad trades is valuable, but each trade that does pass the filter still pays full transaction costs.

### Data Requirements
- TXFD6 tick data with trade-by-trade records (HAVE)
- Markout analysis capability (need to build -- measure price impact 1s, 5s, 10s after each fill)
- Historical fill data from our own order submissions (HAVE for sim mode)

### Estimated Signal Half-Life
Minutes (toxicity is a regime-level property, not tick-level)

### Implementation Complexity
LOW-MEDIUM -- Core algorithm is a linear model with 4 state variables. Main work is: (1) building markout analysis to classify flow toxicity, (2) adapting broker-centric framework to retail trader context, (3) fitting the linear coefficients on historical data.

---

## Papers Reviewed but Rejected

| Paper | arXiv ID | Reason for Rejection |
|-------|----------|---------------------|
| RL-Exec: Impact-Aware RL for Optimal Liquidation | 2511.07434 | Optimal execution for large blocks -- we do not trade large enough to have meaningful price impact on TXFD6 |
| Deep Learning for VWAP Execution in Crypto Markets | 2502.13722 | VWAP/TWAP execution -- irrelevant for our small order sizes |
| Neural HMM with Adaptive Granularity Attention | 2603.20456 | Interesting multi-scale model but: (a) requires LSTM+wavelet+normalizing flow infrastructure we lack, (b) predicts 500ms forward mid-price which is below our 36ms RTT resolution, (c) tested on NASDAQ/LSE/Binance, not Asian futures |
| Explainable Patterns in Crypto Microstructure | 2602.00776 | CatBoost on engineered LOB features -- crypto-specific (maker rebates, no sell tax), uses 1-second frequency too coarse for our tick data |
| DeltaLag: Learning Dynamic Lead-Lag Patterns | 2511.00390 | Lead-lag dead for us per Round 14 (MXFD6 cost 20x > signal) |
| Discovery of a 13-Sharpe OOS Factor | 2511.12490 | Daily cross-sectional equity factor -- wrong timescale and asset class for HFT futures |
| The Red Queen's Trap | 2512.15732 | Post-mortem of failed RL+evolutionary HFT. Confirms: "mathematical impossibility of overcoming microstructure friction without order-flow data." Supports our shift to order-flow approaches |
| Interpretable Hypothesis-Driven Trading | 2512.12924 | Daily OHLCV signals on US equities. 0.55% annualized, Sharpe 0.33. Wrong timescale, weak results |
| Hoeffding's Inequality for Regime Change | 2512.08851 | Theoretical, daily frequency, not actionable for HFT |
| A Deterministic LOB Simulator with Hawkes | 2510.08085 | Simulation framework, not a trading strategy |
| Deep Learning Meets Queue-Reactive (MDQR) | 2501.08822 | LOB simulation model for backtesting, not alpha generation |
| RL in Queue-Reactive Models | 2511.15262 | Optimal execution via RL in simulated LOB. Latency-sensitive, execution focus not alpha |
| Optimal Execution under Liquidity Uncertainty | 2506.11813 | Regime-switching execution theory for large blocks, not our use case |
| Flexible Information Acquisition in Kyle Model | 2603.21842 | Pure theory, no practical trading implications |
| Overreaction as Momentum Indicator | 2602.18912 | Twitter sentiment for AAPL. Wrong market, requires social media data we lack |
| Hawkes-Driven Order Flow in LOB | 2511.18117 | Purely theoretical mesoscopic model, no empirical validation |

---

## Recommended Next Direction for User Selection

### Ranking (most promising first):

1. **Candidate #1: Imbalance Reversal Detection** -- Highest conviction. Directly builds on our Round 15 finding. Supported by the strongest empirical paper in the survey (live trading experiment, not just backtests). Incremental to existing FeatureEngine. Selective maker entries avoid MM structural problems.

2. **Candidate #3: Toxic Flow Detection** -- Best as a COMPLEMENT to #1. Low implementation cost. Provides a defensive filter that improves any strategy. The linear model with data-driven calibration is elegant and practical.

3. **Candidate #2: OFI Regime-Conditional Signal** -- Interesting but riskiest. Depends on whether TXFD6 has meaningful regime variation in OFI predictive power. Could be validated quickly with existing data before committing to implementation.

### Recommended Approach
Implement **Candidate #1 + #3 together** as a single strategy:
- Use toxic flow detection (#3) to identify "safe" trading windows
- Within safe windows, use reversal detection (#1) to time selective maker entries
- This addresses both the "when to trade" and "which direction" questions

### Quick Validation Steps (before full implementation)
1. Compute return autocovariance at 5s windows on existing TXFD6 data -- does it predict reversals?
2. Build markout analysis (1s, 5s, 10s post-fill PnL) on historical fills -- is there measurable toxicity variation?
3. Test regime-conditional OFI (Candidate #2) as a quick data analysis -- does OFI IC vary across volatility regimes?

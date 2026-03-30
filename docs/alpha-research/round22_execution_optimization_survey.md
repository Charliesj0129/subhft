# Round 22: Execution Optimization Literature Survey

## Objective

Survey arXiv for papers on using LOB state for **execution cost reduction** -- not alpha generation, but making existing CBS trades cheaper. If we save 1 point per trade on a 4-point RT cost, that is a 25% cost improvement.

**Context**: TMFD6 futures, CBS strategy (~7 trades/day, contrarian entries), current execution via market orders (paying spread). RT cost ~4 pts.

**Date**: 2026-03-28

---

## Executive Summary

We surveyed 80+ papers across 10 arXiv searches. **12 papers** are directly relevant to execution optimization for thin futures markets. The literature strongly supports the feasibility of LOB-state-conditioned limit/market order switching, with expected savings of 0.5-1.5 pts/trade. However, the critical constraint is **latency**: at ~36ms Shioaji RTT, the window for exploiting favorable LOB states is narrow.

### Key Takeaways

1. **Fill probability is highly predictable** from simple LOB features (R^2 = 0.946 from just queue sizes + imbalance)
2. **Fundamental trade-off**: negative correlation between fill probability and post-fill returns -- high fill probability = adverse selection
3. **Contrarian limit orders work**: placing limit orders against prevailing imbalance captures reversals, but requires precise timing
4. **Latency erodes value**: the benefit of LOB-conditioned placement decays rapidly with latency (Lehalle & Mounjid 2016)
5. **For CBS specifically**: contrarian entries are already favorable for limit orders (buying into selling pressure = thick opposite queue)

### Recommendation

**GO** for a simple LOB-conditioned execution optimizer with 3 features:
- Queue imbalance at best bid/ask
- Spread (in ticks)
- Near-side queue depth

Decision: use limit order when (a) spread >= 2 ticks, (b) imbalance favors fill, (c) time-to-deadline > 3x expected fill time. Otherwise market order with 3-second fallback.

---

## Paper Reviews

---

### P1. Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows

- **ID**: arXiv:2403.02572v2
- **Authors**: Felix Lokin, Fenghui Yu (TU Delft)
- **Published**: 2024-03 (revised 2026-02)
- **Category**: q-fin.TR

**Methodology**: Models the LOB as a collection of interacting queueing systems with state-dependent arrival rates. Derives semi-analytical expressions for fill probabilities using Laplace transforms and continued fractions. The key innovation is state-dependent order flows -- arrival/cancellation rates depend on queue sizes.

**Key Findings**:
- Fill probability at best quote depends primarily on: (1) near-side queue size, (2) opposite-side queue size, (3) their ratio (imbalance)
- Semi-analytical formulas are computationally tractable -- can be evaluated in microseconds
- Fill probabilities at 1 level deeper than best quote are typically negligible
- Validated on FX spot data with good accuracy

**Data Requirements**: L1 order book (bid/ask sizes), tick-level event data for calibration of arrival/cancellation rates.

**Applicability to TMFD6**:
- **HIGH**. The queueing model framework maps well to TAIFEX's FIFO matching. TMFD6 is large-tick (spread usually 1-3 pts), making best-quote fill probability the critical variable.
- The state-dependent rates can be calibrated from our existing ClickHouse tick data.
- **Concern**: FX spot markets have much higher liquidity than TMFD6. Queue sizes are smaller on TMFD6, so fill times will be longer and more variable.

**Extractable for CBS**: Calibrate fill probability as f(Q_near, Q_opp) from historical data. Use as gate: if P(fill | 5s) > 0.7, use limit order; else market order.

---

### P2. Deep Attentive Survival Analysis in Limit Order Books: Estimating Fill Probabilities with Convolutional-Transformers

- **ID**: arXiv:2306.05479v1
- **Authors**: Alvaro Arroyo, Alvaro Cartea, Fernando Moreno-Pino, Stefan Zohren (Oxford)
- **Published**: 2023-06
- **Category**: q-fin.ST

**Methodology**: Survival analysis framework for fill-time prediction. Uses a convolutional-Transformer encoder to map time-varying LOB features to fill-time distributions. Monotonic neural network decoder ensures valid survival functions. Evaluated with proper scoring rules (IBS, CRPS).

**Key Findings**:
- Deep learning significantly outperforms Cox proportional hazards and classical survival models
- LOB snapshots at multiple time lags (looking back ~30s) provide significant predictive power
- Queue position is the single most important feature
- Interpretability analysis shows: spread, imbalance, and recent trade flow are top features after queue position
- Fill probability varies dramatically across assets -- queue dynamics matter more than universal models

**Data Requirements**: L1-L5 order book snapshots at ~100ms frequency, labeled fill/cancel outcomes for training data.

**Applicability to TMFD6**:
- **MEDIUM-HIGH**. The survival analysis framework is well-suited, but the deep learning model is overengineered for our needs (7 trades/day does not justify a Transformer).
- The **feature importance findings** are directly actionable: queue position > spread > imbalance > trade flow.
- We should use a simpler model (logistic regression or small RF) with these same features.

**Extractable for CBS**: Feature ranking for fill probability: (1) queue position/depth, (2) spread, (3) imbalance, (4) recent MO arrival rate. Skip the deep learning, use logistic regression.

---

### P3. KANFormer for Predicting Fill Probabilities via Survival Analysis in Limit Order Books

- **ID**: arXiv:2512.05734v1
- **Authors**: Jinfeng Zhong, Emmanuel Bacry, Agathe Guilloux, Jean-Francois Muzy (Paris-Dauphine)
- **Published**: 2025-12
- **Category**: cs.AI, cs.LG

**Methodology**: Extends Arroyo et al. (P2) by adding agent-level features (order actions) and Kolmogorov-Arnold Networks to the Transformer architecture. Evaluated on **CAC 40 index futures** (Euronext) -- the closest asset class to our TMFD6.

**Key Findings**:
- Agent-level features (individual order submissions, cancellations, modifications) significantly improve fill prediction
- **Queue position** remains the dominant feature at short horizons (<1s)
- At longer horizons (>5s), LOB state features (depth, imbalance) become more important
- Model performs best at short prediction horizons (<0.6s). At 120s horizons, performance degrades to near-random (AUC -> 0.5)
- Traditional models (Cox, Random Forest) outperform deep learning at longer horizons
- SHAP analysis reveals time-varying feature importance -- different features matter at different fill horizons

**Data Requirements**: Labeled order data with fill/cancel outcomes, L1+ LOB snapshots, individual order actions.

**Applicability to TMFD6**:
- **HIGH** -- tested on index futures, directly comparable asset class
- Critical insight: **at CBS time horizons (5-300s), simple models work as well as deep learning**
- The degradation to AUC~0.5 at 120s suggests fill prediction beyond ~30s is largely noise on index futures
- This means: for CBS (300s hold), we should only use limit orders with SHORT fill deadlines (5-10s), then fall back to market orders

**Extractable for CBS**: Use limit order with 5-10s deadline. If not filled, cancel and send market order. Fill prediction model only needs to be accurate at short horizons where simple features work.

---

### P4. The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off

- **ID**: arXiv:2502.18625v2
- **Authors**: Jakob Albers, Mihai Cucuringu, Sam Howison, Alexander Shestopaloff (Oxford)
- **Published**: 2025-02
- **Category**: q-fin.TR

**Methodology**: Live trading experiment on Binance BTC perpetual. Empirically studies the relationship between fill probability, queue position, and post-fill returns. Models "Reversals" where contrarian maker strategies work.

**Key Findings**:
- **Fundamental trade-off documented**: negative correlation between fill probability and post-fill returns
- Fill probability decreases with near-side queue size, increases with opposite-side queue size
- Simple OLS on (Q_near, Q_opp, imbalance) achieves **R^2 = 0.946** for fill probability -- minimal model needed
- Viable maker strategies require **contrarian** approach: counter-trading prevailing imbalance
- "Reversals" (where contrarian limit orders profit) occur when:
  - Imbalance is extreme (one-sided book)
  - Recent price move was impulsive (likely to revert)
  - Spread is >= 2 ticks
- Fill probabilities range from 30% (unfavorable conditions) to 90%+ (favorable)
- Queue position matters enormously -- front-of-queue vs back-of-queue can be 3x difference in fill rate

**Data Requirements**: L1 order book (bid/ask sizes), trade flow data. Minimal requirements.

**Applicability to TMFD6**:
- **CRITICAL PAPER FOR CBS**. CBS is already a contrarian strategy (buying after cascade drops). The LOB conditions at CBS entry points should naturally favor limit orders:
  - After a 40bps cascade, selling pressure creates thick ask queue (high Q_opp for buy limit)
  - Imbalance is skewed bearish, which is exactly the contrarian fill setup
  - Post-fill returns should be favorable (mean reversion is the CBS thesis)
- R^2 = 0.946 from 3 features means we do NOT need ML -- a simple lookup table or linear model suffices
- **WARNING**: This is BTC perpetual data (extremely liquid, tight spreads). TMFD6 has wider spreads and thinner books, so fill rates will be lower. Must validate empirically.

**Extractable for CBS**:
- CBS entries are naturally "Reversal" setups per this paper's taxonomy
- Decision rule: if Q_opp/Q_near > 2 AND spread <= 2 ticks AND CBS signal active -> limit order
- Otherwise -> market order
- Expected savings: half-spread on ~50% of trades = ~0.5-0.75 pts/trade average

---

### P5. Interpretable ML for High-Frequency Execution

- **ID**: arXiv:2307.04863v2
- **Authors**: Timothee Fabre, Vincent Ragel
- **Published**: 2023-07
- **Category**: q-fin.TR

**Methodology**: Uses microstructural features with a simple neural network to predict fill probability at fixed horizons. Key innovation: weighted loss function for censored data, and **clean-up cost estimation** for non-execution scenarios.

**Key Findings** (from abstract and search context):
- Strong state dependence of fill probability on LOB features
- Compares crypto CEX vs Euronext equities -- feature importances differ significantly between asset classes
- **Clean-up cost** (the cost of executing via market order after a limit order fails) can be well approximated by a smooth function of market features
- Practical framework: decision to post limit vs execute immediately, AND optimal distance of placement
- Backtesting without hypothetical order insertion -- uses only real orders to avoid overfitting

**Data Requirements**: L1+ LOB features, labeled trade data, clean-up cost estimation.

**Applicability to TMFD6**:
- **HIGH**. The clean-up cost framework is exactly what we need. CBS has a time constraint (entry must happen within the signal window). If limit order does not fill, the clean-up cost (market order at potentially worse price) must be modeled.
- Feature importance comparison across asset classes warns us: do NOT blindly import features from equity/crypto research. Must validate on TMFD6.
- The simple architecture (for HF execution speed) aligns with our latency constraints.

**Extractable for CBS**: Model clean-up cost = f(spread, volatility, time_remaining). This determines the breakeven fill probability threshold: use limit if P(fill) * savings > (1 - P(fill)) * cleanup_cost.

---

### P6. Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency

- **ID**: arXiv:1610.00261v4
- **Authors**: Charles-Albert Lehalle, Othmane Mounjid (Capital Fund Management)
- **Published**: 2016-10
- **Category**: q-fin.TR

**Methodology**: Three-part study: (1) empirical analysis of limit order acceptance as function of imbalance, (2) stochastic control framework for limit order placement under adverse selection, (3) quantification of how latency erodes limit order value.

**Key Findings**:
- Market participants already condition limit order submission on LOB imbalance -- this is empirically documented
- Adverse selection is paramount: if price is about to drop, your buy limit fills easily but you lose money
- **Optimal limit order placement depends on a critical time threshold t0**: for horizons > t0, placing away from the best quote is optimal (better price + acceptable fill probability). For t < t0, best quote or market order is optimal.
- **Latency cost quantified**: the value of predicting future order flow is eroded by latency. With higher latency, you cannot cancel/reinsert fast enough to avoid adverse selection.
- This provides a **rational basis for why speed matters** for limit order placement -- not for alpha, but for protection.

**Data Requirements**: L1 order book, order flow data, latency measurements.

**Applicability to TMFD6**:
- **CRITICAL for our latency constraint**. At 36ms Shioaji RTT:
  - We CANNOT react to adverse selection within a tick (TMFD6 median tick interval = 125ms)
  - But we CAN make the initial placement decision based on LOB state at signal time
  - The critical time t0 should be calibrated for TMFD6 -- likely 2-5 seconds given our spreads
- Key insight: **we should NOT try to dynamically manage limit orders** (cancel/reinsert). Our latency is too high. Instead: place-and-wait with a fixed timeout.
- "Place at best bid/ask if LOB state is favorable, with N-second timeout, then market order" is the practical policy given our latency.

**Extractable for CBS**: Do NOT attempt dynamic limit order management. Use fire-and-forget with timeout. Latency-safe policy: evaluate LOB state once at signal time, decide limit/market, commit.

---

### P7. Optimal Order Placement in Limit Order Markets

- **ID**: arXiv:1210.1625v4
- **Authors**: Rama Cont, Arseniy Kukanov (Columbia)
- **Published**: 2012-10
- **Category**: q-fin.TR, q-fin.PM

**Methodology**: Formulates order placement as a convex optimization problem. Studies the optimal split between limit and market orders based on: LOB state, fee structure, order flow properties, trader preferences.

**Key Findings**:
- For a single exchange, derives **explicit closed-form solution** for optimal limit/market split
- The optimal split depends on: (1) queue length at best, (2) fill probability, (3) fee differential between maker/taker, (4) urgency
- With maker rebates, limit orders are strongly preferred when queue is short (<50% of typical)
- Without maker rebates (our case on TAIFEX), the threshold shifts: limit orders are only preferred when fill probability is high (>70%)
- Multi-exchange case (not relevant to us) solved via stochastic algorithm

**Data Requirements**: Queue sizes, fill probability estimates, fee schedule.

**Applicability to TMFD6**:
- **HIGH**. The closed-form solution can be directly applied.
- **TAIFEX has NO maker rebates** -- we pay the same fee whether maker or taker. This means the only benefit of limit orders is **spread capture** (avoiding crossing the spread).
- Without maker rebates, the breakeven fill probability is higher. Rule of thumb: limit order only when P(fill | deadline) > 70% and spread >= 2 ticks (saving > fee + adverse selection risk).

**Extractable for CBS**: Since TAIFEX has no maker/taker fee differentiation, the only benefit is spread savings. Threshold: P(fill|5s) > 0.7 AND spread >= 2 pts.

---

### P8. The Effect of Latency on Optimal Order Execution Policy

- **ID**: arXiv:2504.00846v2
- **Authors**: Chutian Ma, Giacinto Paolo Saggese, Paul Smith
- **Published**: 2025-04
- **Category**: q-fin.MF, math.OC

**Methodology**: Stochastic optimal control where a risk-averse trader balances profit vs risk. Models price uncertainty with Brownian motion, derives closed-form approximations for fill probability as a function of limit price and latency.

**Key Findings**:
- Derives closed-form relationship: fill probability = f(limit_price, time_horizon, volatility, **latency**)
- Latency introduces a **third risk**: orders intended as limit orders may execute as market orders if the price moves during submission latency
- With high latency, optimal strategy shifts toward more conservative (wider) limit prices or pure market orders
- Mean-variance framework quantifies the risk tolerance needed to justify limit orders at given latency
- Key equation: expected cost = fill_prob * (spread_saving) + (1 - fill_prob) * cleanup_cost + latency_risk * adverse_cost

**Data Requirements**: Volatility estimates, latency measurements, spread distribution.

**Applicability to TMFD6**:
- **MEDIUM-HIGH**. The latency risk model directly applies. At 36ms RTT:
  - Price can move 1-2 pts during our round-trip latency
  - A limit buy at best bid could become a market order if best ask drops during submission
  - This "latency execution risk" must be added to our cost model
- The closed-form approximations enable real-time decision making without complex optimization.

**Extractable for CBS**: Add latency risk to the limit/market decision: if realized_vol_1s > threshold, prefer market order (price moves too fast for limit placement to be safe).

---

### P9. Instantaneous Order Impact and High-Frequency Strategy Optimization in Limit Order Books

- **ID**: arXiv:1707.01167v2
- **Authors**: Federico Gonzalez, Mark Schervish (Carnegie Mellon)
- **Published**: 2017-07
- **Category**: q-fin.TR

**Methodology**: Markov chain model of LOB dynamics that incorporates the type of the most recent order. Frames optimal order placement as a Markov Decision Process (MDP). Derives optimal policy over {limit order, market order, cancel} actions.

**Key Findings**:
- The type of the last order (buy MO, sell MO, buy LO, sell LO, cancel) significantly alters future order arrival rates
- Optimal policy uses **all three order types**: limit orders in favorable states, cancellations when adverse selection rises, market orders when mid-price is about to move
- LOB state summarized by just 2 features: (1) volume at bid/ask, (2) type of most recent order
- The optimal policy significantly outperforms: (a) always-market, (b) always-limit, (c) limit-with-timeout
- Key insight: **cancellation is a critical action** -- the ability to cancel a non-filling limit order before adverse price movement is as valuable as the limit order itself

**Data Requirements**: Ultra-HF order-by-order data (ITCH/PITCH level), LOB state at each event.

**Applicability to TMFD6**:
- **MEDIUM**. The MDP framework is elegant but requires:
  - Ultra-HF event data (we have ticks, not individual order events from TAIFEX)
  - Low latency for the cancellation action (36ms is too slow for reactive cancellation on TMFD6)
- The insight about "most recent order type" is partially applicable -- we can observe recent trade direction from our tick stream.
- The cancellation value insight reinforces P6's conclusion: **at our latency, we cannot effectively cancel**. This pushes us toward "limit-with-timeout" (a strategy this paper shows is suboptimal, but it assumes sub-millisecond cancellation).

**Extractable for CBS**: Use recent trade direction as a feature (last 5 trades net buy/sell). If recent flow is same direction as our intended trade -> avoid limit order (momentum, likely to sweep our level).

---

### P10. Market Simulation under Adverse Selection

- **ID**: arXiv:2409.12721v2
- **Authors**: Luca Lalor, Anatoliy Swishchuk (University of Calgary)
- **Published**: 2024-09
- **Category**: q-fin.CP

**Methodology**: Studies how fill probabilities and adverse fills affect trading strategy performance in simulation. Tests on ES, NQ, CL, ZN futures (CME). Proposes a "prudent simulation framework" that accounts for adverse selection.

**Key Findings**:
- **Simulating market orders independently of price processes massively inflates backtest performance** -- this is a critical backtesting bias
- Adverse fills (fills that immediately lose money) account for a significant fraction of all limit order fills
- Incorporating realistic fill probabilities AND adverse selection reduces simulated profits by 30-70% vs naive simulation
- ES/NQ (liquid) have lower adverse fill rates than CL/ZN (thinner markets)
- Thin markets (like ZN) show higher adverse selection per fill -- limit orders are more dangerous

**Data Requirements**: Order-level data with fill attribution, price movements around fills.

**Applicability to TMFD6**:
- **HIGH** for backtesting methodology. TMFD6 is comparable to ZN (thin government futures).
- **Critical warning**: any backtest of limit order execution on TMFD6 must account for adverse selection. Naive fill assumption (price touches our level = fill) will overstate savings.
- Recommended simulation: use P(fill | queue_position, Q_total, time) AND condition post-fill returns on the fill event (adverse selection adjustment).

**Extractable for CBS**: When backtesting the execution optimizer, use adversity-aware simulation: fills only count when queue would realistically reach our position, and post-fill returns must be adjusted for the information content of the fill itself.

---

### P11. Optimal Placement of a Small Order in a Diffusive Limit Order Book

- **ID**: arXiv:1708.04337v1
- **Authors**: Jose Figueroa-Lopez, Hyoeun Lee, Raghu Pasupathy (Purdue/Washington)
- **Published**: 2017-08
- **Category**: q-fin.TR

**Methodology**: Studies optimal limit order placement for a trader who must clear inventory by time T, choosing between limit and market orders. Derives optimal placement policy for diffusive markets.

**Key Findings**:
- Under negative drift (price moving against you), there exists a critical time t0 such that for t > t0, optimal placement is NOT at the best quote but deeper in the book
- This is counterintuitive: when you have time, placing deeper offers better expected cost
- For short horizons (t < t0), best quote or market order is optimal
- The critical time t0 depends on drift, volatility, and spread
- Simple approximation method for t0 provided

**Data Requirements**: Drift estimate, volatility, spread.

**Applicability to TMFD6**:
- **MEDIUM**. The "place deeper" result is interesting but likely not practical for TMFD6:
  - TMFD6 spread is already 1-3 ticks. "Deeper" placement means only 1-2 ticks improvement.
  - At thin liquidity, deeper placement has negligible fill probability
  - The CBS time constraint (signal validity ~300s) sets a hard deadline
- The critical time t0 concept IS useful: calibrate t0 for TMFD6 to know when to switch from "try limit" to "go market."

**Extractable for CBS**: Calibrate critical time t0 -- the latest you can switch from limit to market order. For CBS with 300s hold, if limit order hasn't filled by t0 (~30-60s estimate), send market order.

---

### P12. Optimal High Frequency Trading in a Pro-Rata Microstructure with Predictive Information

- **ID**: arXiv:1205.3051v1
- **Authors**: Fabien Guilbaud, Huyen Pham (Paris Diderot)
- **Published**: 2012-05
- **Category**: q-fin.TR

**Methodology**: Framework for optimal trading in **pro-rata** limit order books (as used in short-term interest rate futures). Combines impulse controls (market orders) and regular controls (limit orders). Models partial fills and overtrading risk.

**Key Findings**:
- In pro-rata markets, limit orders are only partially filled (proportional to your size vs queue total)
- Optimal strategy uses predictive information about mid-price direction to switch between limit and market orders
- When directional signal is strong -> market order (capture the move)
- When signal is weak/absent -> limit order (collect spread while waiting)
- Overtrading risk (excessive inventory from partial fills) must be managed

**Data Requirements**: LOB data, directional signal, fill rate calibration.

**Applicability to TMFD6**:
- **LOW-MEDIUM**. TAIFEX uses **FIFO** matching, not pro-rata. The partial fill mechanics are different.
- However, the signal-conditioned switching framework is relevant: CBS has a directional view (contrarian). When CBS confidence is high -> tolerate worse execution to ensure fill. When CBS is marginal -> patient limit order.
- The overtrading risk is not relevant (CBS trades 1 contract at a time).

**Extractable for CBS**: Signal strength should modulate execution aggressiveness. Strong CBS signal (large cascade) -> market order for certainty. Weak/borderline signal -> limit order (additional cost savings partially compensate for lower confidence).

---

## Synthesis: Practical CBS Execution Optimizer Design

### Features Required (ranked by importance)

| Feature | Source | Computation |
|---------|--------|-------------|
| 1. Queue depth at best bid/ask | L1 LOB | Direct from BidAskEvent |
| 2. Spread (in ticks) | L1 LOB | ask_1 - bid_1 |
| 3. Imbalance = (Q_bid - Q_ask)/(Q_bid + Q_ask) | L1 LOB | From BidAskEvent |
| 4. Recent trade direction (net buys in last 5 trades) | Tick stream | Rolling count from TickEvent |
| 5. Short-term volatility (1s realized vol) | Tick stream | Rolling std of returns |

All features are already available in the existing FeatureEngine or trivially derivable from current events. **No new data sources needed.**

### Decision Framework

```
ON CBS_SIGNAL(side, urgency):
    spread = current_spread_ticks()
    imbalance = current_imbalance()
    q_near = queue_at_our_entry_side()
    q_opp = queue_opposite_side()
    vol_1s = recent_volatility()

    # Rule 1: Spread too narrow -- no benefit to limit order
    if spread < 2:
        return MARKET_ORDER

    # Rule 2: Volatility too high -- latency risk
    if vol_1s > VOL_THRESHOLD:
        return MARKET_ORDER

    # Rule 3: Favorable imbalance for fill (contrarian setup)
    fill_score = q_opp / max(q_near, 1)
    if fill_score > 1.5 and spread >= 2:
        return LIMIT_ORDER(timeout=5s, fallback=MARKET_ORDER)

    # Rule 4: Unfavorable imbalance -- unlikely to fill in time
    if fill_score < 0.5:
        return MARKET_ORDER

    # Rule 5: Marginal case -- use limit with short timeout
    if spread >= 3:
        return LIMIT_ORDER(timeout=3s, fallback=MARKET_ORDER)

    return MARKET_ORDER
```

### Expected Savings

Conservative estimate based on paper findings:
- ~50% of CBS entries will have spread >= 2 AND favorable imbalance (contrarian entry into selling pressure)
- Of those, ~60% will fill as limit orders within 5s (based on P4 fill probability ranges)
- Spread saving per limit fill: ~1.5 pts (half of typical 3-pt spread)
- Net saving: 50% * 60% * 1.5 = **0.45 pts/trade average**
- On 7 trades/day * 220 days: **693 pts/year saved**
- At TMFD6 10 NTD/pt: **6,930 NTD/year per contract**

This is a 11% reduction in round-trip costs (0.45/4.0).

### Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Adverse selection on fills | CBS is already contrarian; fills into selling should mean-revert (the CBS thesis). Monitor post-fill slippage. |
| Non-fill forcing late market order at worse price | Fixed 5s timeout limits exposure. Cleanup cost bounded by spread + 1 tick worst case. |
| Latency (36ms RTT) prevents cancellation | Fire-and-forget policy -- no dynamic management. Latency only affects initial placement. |
| Backtest overfit (naive fill assumption) | Use adverse-selection-aware backtest per P10. Only count fills when queue position is realistic. |
| Thin market (TMFD6 < ES/NQ) | Conservative fill probability thresholds. Empirically validate on March 2026 data before deployment. |

### Implementation Complexity

- **LOC estimate**: ~150 lines (ImbalanceTimer module)
- **New features needed**: 0 (all available from existing FeatureEngine)
- **New dependencies**: 0
- **Risk to existing strategy**: Zero -- execution optimizer sits BELOW strategy layer
- **Rollback**: Trivial -- `HFT_EXEC_OPTIMIZER_ENABLED=0` disables, falls back to market orders

### Validation Plan

1. **Phase 1**: Historical analysis -- for each CBS signal in March 2026, compute LOB state features and counterfactual fill outcomes
2. **Phase 2**: Paper trade -- run execution optimizer in shadow mode, log decisions without executing
3. **Phase 3**: Live validation -- enable with conservative thresholds, compare execution costs vs pure-market baseline

---

## Papers Reviewed but Not Applicable

| ID | Title | Reason for Exclusion |
|----|-------|---------------------|
| 1807.01428 | Trading Cointegrated Assets with Price Impact | Multi-asset execution, basket liquidation -- not relevant to single-instrument CBS |
| 1912.01129 | Market Making in Dark Pools (RL) | Dark pool + maker incentives -- TAIFEX is single venue, no dark pools |
| 2211.06046 | Are Large Traders Harmed by Front-running HFTs? | Theoretical Kyle model, no actionable execution framework |
| 1906.02312 | Risk-Sensitive Decision Trees for Execution | RL-based execution in ABIDES simulator -- interesting but requires full simulator environment |
| 2006.05574 | Multi-Agent RL for LOB Execution | ABIDES-based RL, converges to TWAP -- not applicable to single-trade execution |
| 1205.3051 | Optimal HF Trading in Pro-Rata Microstructure | Pro-rata matching (not TAIFEX's FIFO). Conceptual overlap only. |
| 1908.04333 | Random Walk Model for Algorithmic Trading | Shows no optimal limit level under random walk -- baseline null result. Our CBS has directional view (contrarian), so this does not apply. |

---

## References

1. Lokin, F. & Yu, F. (2024). "Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows." arXiv:2403.02572v2.
2. Arroyo, A., Cartea, A., Moreno-Pino, F., & Zohren, S. (2023). "Deep Attentive Survival Analysis in Limit Order Books." arXiv:2306.05479v1.
3. Zhong, J., Bacry, E., Guilloux, A., & Muzy, J.-F. (2025). "KANFormer for Predicting Fill Probabilities via Survival Analysis in Limit Order Books." arXiv:2512.05734v1.
4. Albers, J., Cucuringu, M., Howison, S., & Shestopaloff, A. (2025). "The Market Maker's Dilemma: Fill Probability vs. Post-Fill Returns Trade-Off." arXiv:2502.18625v2.
5. Fabre, T. & Ragel, V. (2023). "Interpretable ML for High-Frequency Execution." arXiv:2307.04863v2.
6. Lehalle, C.-A. & Mounjid, O. (2016). "Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency." arXiv:1610.00261v4.
7. Cont, R. & Kukanov, A. (2012). "Optimal Order Placement in Limit Order Markets." arXiv:1210.1625v4.
8. Ma, C., Saggese, G.P., & Smith, P. (2025). "The Effect of Latency on Optimal Order Execution Policy." arXiv:2504.00846v2.
9. Gonzalez, F. & Schervish, M. (2017). "Instantaneous Order Impact and HF Strategy Optimization in LOBs." arXiv:1707.01167v2.
10. Lalor, L. & Swishchuk, A. (2024). "Market Simulation under Adverse Selection." arXiv:2409.12721v2.
11. Figueroa-Lopez, J., Lee, H., & Pasupathy, R. (2017). "Optimal Placement of a Small Order in a Diffusive LOB." arXiv:1708.04337v1.
12. Guilbaud, F. & Pham, H. (2012). "Optimal HF Trading in a Pro-Rata Microstructure with Predictive Information." arXiv:1205.3051v1.

# R18 Stage 1: Paper Survey — TMFD6 Conditional Market Making

**Date**: 2026-03-26
**Researcher**: Claude (Researcher Agent)
**Status**: Complete
**Scope**: OBI/microprice conditional trading, selective passive LP, inventory-bounded hybrid

---

## Executive Summary

This survey covers 3 candidate directions for TMFD6 (Mini-TAIEX futures), grounded in 18+ papers from q-fin.TR/MF literature (2008-2025). All directions share a common premise: **selective, conditional passive strategies** that only engage when spread economics are favorable -- a structurally different approach from R12-R17's aggressive taker strategies that uniformly failed on TMFD6's cost structure.

**Key innovation vs R12-R17**: Prior rounds sought L1 signals strong enough to overcome 4pt RT cost as a taker. This round focuses on **earning the spread** (passive maker) under selective conditions, converting the wide-spread regime from "adverse selection trap" (R16 finding on TXFD6) into "edge opportunity" (TMFD6's 45.5% profitable-spread time).

### Converged Candidates (3 directions)

| # | Direction | Core Mechanism | Key Differentiator from R12-R17 |
|---|-----------|---------------|-------------------------------|
| **A** | Reversal-Conditional Maker (RCM) | Contrarian limit orders when OBI falsely predicts next move | Passive fills + spread capture; signal needed only for filtering, not prediction |
| **B** | Spread-Gated Selective LP (SG-LP) | Quote only when spread >= 5 pts; microprice-adjusted skew | Cost threshold eliminates unprofitable regimes; 45.5% eligible time on TMFD6 |
| **C** | Inventory-Bounded Hybrid (IBH) | A-S framework with hard 1-2 lot cap + phi_8min filter | Combines A+B with formal inventory control; suitable for retail constraints |

---

## Direction A: Reversal-Conditional Maker (RCM)

### Core Mechanism

Post contrarian limit orders at the touch when the order book imbalance (OBI) is predicted to *incorrectly* forecast the next price move. This exploits the fundamental fill-probability vs. post-fill-return trade-off documented by Albers et al. (2025).

**Signal logic**: When bid queue >> ask queue (price-positive imbalance), the conventional wisdom is "price will rise." A contrarian **sell** limit order at the ask has high fill probability (short queue = good queue position). If the imbalance signal is *wrong* (a "reversal"), the sell order fills AND price drops -- profitable trade. The challenge is identifying when reversals occur.

### Paper References

1. **Albers, Cucuringu, Howison, Shestopaloff (2025)** -- "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off" [arXiv:2502.18625]
   - **Live experiment**: 232,897 minimum-sized maker orders on Binance BTC perpetual over 1 week
   - **Key finding**: Fundamental negative correlation between fill probability and post-fill returns. ~15% of high-fill-probability orders are "reversals" (imbalance incorrectly predicts next move)
   - **Reversal model**: Logistic regression on 4 feature groups (price dynamics, LOB state, recent trades, queue survival times). At 0.24 reversal probability threshold: **+0.71 bp/roundtrip** (vs -0.44 bp naive), Sharpe 11.97 (annualized), 654 roundtrips/day
   - **Critical caveat**: Results on minimum-size orders only; performance degrades with larger sizes. Maker rebate (0.5 bp/leg) assumed.

2. **DeLise (2024)** -- "The Negative Drift of a Limit Order Fill" [arXiv:2407.16527]
   - Documents that majority of maker fills on Treasury bond futures (ZN) are adverse -- price moves against the maker post-fill
   - Quantifies the "negative drift" as a function of queue position and fill mechanism
   - **Relevance**: Confirms the adversity problem is universal across asset classes (crypto, bonds, equity futures)

3. **Gould & Bonart (2015)** -- "Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book" [arXiv:1512.03492]
   - Establishes OBI as a statistically significant predictor of next mid-price move direction on NASDAQ
   - Logistic regression shows strongly significant relationship between queue imbalance and price direction
   - **Relevance**: Baseline predictor; RCM strategy requires predicting when this baseline *fails*

4. **Blakely (2024)** -- "High resolution microprice estimates from limit orderbook data using hyperdimensional vector Tsetlin Machines" [arXiv:2411.13594]
   - Extends Stoikov's microprice with higher-order imbalance dynamics
   - Shows higher LOB levels improve microprice accuracy
   - **Relevance**: Microprice as fair-value anchor for RCM entry/exit decisions

5. **Stoikov (2018)** -- "The micro-price: a high-frequency estimator of future prices" [SSRN:2970694, Quantitative Finance 18(12)]
   - Defines microprice as imbalance-weighted mid-price adjustment; martingale by construction
   - Better short-term predictor than mid-price or VWAP mid
   - **Relevance**: Foundation for fair-value estimation in all 3 directions

### Expected IC Range

- **Reversal prediction accuracy**: ~15% base rate for reversals; model lifts to ~25-40% true positive rate at useful thresholds (per Albers et al.)
- **Post-fill return improvement**: +0.5 to +1.5 bp per roundtrip at moderate thresholds (Albers: +0.71 bp at 0.24 threshold on BTC perp)
- **TMFD6 translation uncertainty**: HIGH. BTC perpetual has 1-tick spread ~100% of time, TMFD6 has variable spread. The wider-spread regime may actually *help* (more room for positive returns per roundtrip) or *hurt* (slower fills, more adverse selection in transition)

### Why Different from R12-R17 Failures

- R12-R17 used OBI as a directional *taker* signal. RCM uses OBI *incorrectness* as a *maker* filter
- No need for signal to predict direction with IC > cost threshold. Only needs to identify when OBI is wrong ~25% of the time
- Earns the spread instead of paying it -- fundamentally different cost structure
- TMFD6's 45.5% wide-spread time provides the economic room that TXFD6 lacked

### Key Risks

1. **Latency**: Albers' results assume competitive queue position. TMFD6 queue depth is 4.1 lots (small), so queue priority matters less than on deep books, but 36ms RTT still means ~65ms round-trip to react to LOB changes
2. **Feature transferability**: Reversal features calibrated on BTC perpetual may not transfer to TMFD6 (different market microstructure, participant mix, tick structure)
3. **Regime dependence**: Reversal frequency may vary with market conditions (opening vs midday)
4. **Maker rebate assumption**: Albers' strategy gains +1bp from maker rebates. TMFD6 has no maker rebates for retail (confirmed R16 finding). This eliminates a significant portion of the edge.

### Data Requirements

- **L1 sufficient for basic implementation** (bid/ask size, mid-price, spread)
- L2 (depth levels 2-5) useful for enhanced microprice but not strictly required
- Trade-level data needed for "recent trades" feature group
- **Already available**: All L1 data in ClickHouse (9.16M rows, 58 days)

---

## Direction B: Spread-Gated Selective LP (SG-LP)

### Core Mechanism

Only provide liquidity (post limit orders) when the bid-ask spread exceeds a profitability threshold. On TMFD6, this means quoting only when spread >= 5 pts (45.5% of time), ensuring each filled roundtrip has a positive expected P&L before any signal is applied.

**Key insight**: At spread = 4 pts (median), the market maker earns 4 pts per roundtrip but pays 4 pts in fees -- zero edge. At spread >= 5 pts, each roundtrip has built-in 1+ pt edge *before* adverse selection. The strategy only needs to avoid the worst adverse selection scenarios to be profitable.

### Paper References

1. **Gueant, Lehalle, Fernandez-Tapia (2013)** -- "Dealing with the Inventory Risk: A solution to the market making problem" [arXiv:1105.3115]
   - Closed-form solution for optimal bid/ask quotes under inventory risk
   - Transforms HJB equations into linear ODE system under inventory constraints
   - **Key formula**: Optimal spread = gamma * sigma^2 * (2q - Q_max) + 2/k * ln(1 + k/A), where gamma = risk aversion, q = inventory, k and A = order arrival parameters
   - **Relevance**: Theoretical foundation for spread-dependent quoting. The formula shows optimal spread *widens* with inventory and volatility -- SG-LP implements this by only quoting when market spread exceeds the theoretical minimum

2. **Cartea, Jaimungal, Ricci (2014)** -- "High-frequency market-making with inventory constraints and directional bets" [arXiv:1206.4810]
   - Extends A-S to general mid-price processes with inventory constraints
   - Allows directional bets (skewed quotes) alongside market making
   - **Key finding**: Inventory penalty parameter gamma controls the trade-off between capturing spread and managing inventory risk. Higher gamma -- tighter inventory, narrower quotes, lower risk
   - **Relevance**: Provides the skew mechanism -- when microprice diverges from mid-price, skew quotes toward the microprice to improve fills on the "right" side

3. **Cartea & Wang (2020)** -- "Market Making with Alpha Signals" [IJTAF 23(3)]
   - Shows how to incorporate alpha signals (short-term price predictions) into market making quotes
   - Alpha signal used to: (a) reduce adverse selection, (b) take directional positions, (c) manage inventory
   - **Relevance**: Provides the theoretical framework for combining SG-LP with phi_8min or OBI as directional skew signals

4. **Wang, Ventre, Polukarov (2023)** -- "Robust Market Making: To Quote, or not To Quote" [arXiv:2508.16588, ICAIF'23]
   - Uses adversarial RL to train market makers that can choose NOT to quote
   - Shows selective quoting (including single-sided quotes) outperforms continuous two-sided quoting
   - **Key finding**: Agents trained with adversarial conditions learn to skip quoting in high-volatility regimes -- robust out-of-sample
   - **Relevance**: Theoretical validation that selective LP outperforms continuous LP, especially under regime uncertainty

5. **Feldman & Maier-Paape (2025)** -- "Optimal Quoting under Adverse Selection and Price Reading" [arXiv:2508.20225]
   - Models adverse selection from informed traders + "price reading" (market maker's quotes reveal inventory)
   - At zero inventory: spread widens as compensation for information risk
   - At nonzero inventory: more aggressive skewing to reduce time in unbalanced positions
   - **Relevance**: TMFD6's small queue depth (4.1 lots) means inventory is more visible -- price reading risk is real. Strategy must manage inventory visibility.

### Expected IC Range

- **Spread capture**: When spread = 5 pts, gross capture = 5 pts, net of 4 pts RT cost = 1 pt (~3.3 NTD). When spread = 10 pts, net = 6 pts (~20 NTD)
- **Average profitable spread on TMFD6**: 19.7 pts when spread >= 5 pts -- theoretical average capture of 15.7 pts before adverse selection
- **Realistic estimate**: After adverse selection (50-70% of fills are adverse per DeLise), net capture is likely 3-8 pts per profitable roundtrip, 0 to -4 pts per adverse roundtrip
- **Overall P&L depends critically on adverse selection rate**: If < 60% adverse -- likely profitable. If > 70% adverse -- losing even with spread gate
- **Uncertainty**: HIGH. No direct precedent for TMFD6-specific adverse selection rates at wide spreads

### Why Different from R12-R17 Failures

- R16 found "wide spread = adverse selection trap" on **TXFD6** where spread >= 5 only 2.1% of time (rare, hence informationally loaded). On TMFD6 it's 45.5% (common, hence less informationally loaded)
- SG-LP doesn't need a strong directional signal -- it needs spread > cost, which is a simple filter
- Previous rounds never tested **passive** strategies on TMFD6. All R12-R17 strategies were taker-based
- The 45.5% eligible time on TMFD6 was explicitly identified but never backtested (R16 finding)

### Key Risks

1. **Adverse selection at wide spreads**: The key question is whether TMFD6's wide-spread periods are adverse-selection-loaded (informed traders cause spread widening) or benign (low liquidity periods). R16 showed adverse selection on TXFD6 wide spreads, but TMFD6 is structurally different (smaller contract, retail-dominated)
2. **Fill rate in wide-spread regime**: If spread is wide because there's no counterparty, limit orders won't fill. Need to verify that wide-spread periods on TMFD6 have sufficient order flow
3. **Queue position competition**: Even at 4.1 lots average depth, if HFT firms are at front-of-queue, retail always gets adverse fills at back
4. **No maker rebates**: TMFD6 on TAIFEX has no maker rebate program for retail. The entire edge must come from spread capture minus adverse selection

### Data Requirements

- **L1 sufficient**: Spread, bid/ask sizes, trade prices
- Need to measure: distribution of spread durations, order arrival rates during wide-spread periods, adverse selection rate conditional on spread
- **All available in ClickHouse**: 9.16M rows over 58 days

---

## Direction C: Inventory-Bounded Hybrid (IBH)

### Core Mechanism

Implements a constrained Avellaneda-Stoikov market making framework with:
1. **Hard inventory cap**: Maximum 1-2 lots (retail risk constraint)
2. **Spread gate** from Direction B (only quote when spread >= 5 pts)
3. **Directional filter** from phi_8min (IC=0.041, orthogonal to OFI, identified R17) or reversal signal from Direction A
4. **Aggressive inventory unwind**: When at cap, immediately cross spread to flatten

This is the practical synthesis that maps academic MM theory to TMFD6 retail constraints.

### Paper References

1. **Avellaneda & Stoikov (2008)** -- "High-frequency trading in a limit order book" [Quantitative Finance 8(3)]
   - **THE** foundational paper. Derives optimal bid/ask quotes as function of inventory q, volatility sigma, time horizon T, risk aversion gamma
   - Key formula: reservation price = S - q * gamma * sigma^2 * (T-t), optimal spread = gamma * sigma^2 * (T-t) + (2/gamma) * ln(1 + gamma/k)
   - **Relevance**: Direct framework for IBH. With TMFD6 parameters: sigma ~ 0.1%/min, gamma calibrated to 1-2 lot cap, T = trading session

2. **Gueant, Lehalle, Fernandez-Tapia (2013)** -- [arXiv:1105.3115] (see Direction B)
   - Closed-form solution under inventory constraints -- directly applicable to hard 1-2 lot cap

3. **Zhang (2024)** -- "Adaptive Optimal Market Making Strategies with Inventory Liquidation Cost" [arXiv:2405.11444]
   - Introduces liquidation cost into MM optimization
   - Key innovation: demand functions with *random* coefficients model partial fill variability
   - Closed-form solution in discrete time -- practical for implementation
   - **Relevance**: The "liquidation cost" maps directly to TMFD6's scenario: when at inventory cap, must cross spread (4 pts cost) to unwind. This cost should be modeled explicitly.

4. **Lokin & Yu (2024)** -- "Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows" [arXiv:2403.02572]
   - Semi-analytical fill probability expressions as function of queue sizes and order flow rates
   - Models LOB as interacting queuing systems
   - **Relevance**: Needed to estimate fill rates for TMFD6's thin book. If fill rate is too low, the strategy spends too long exposed to inventory risk.

5. **Cont, Stoikov, Talreja (2010)** -- "A Stochastic Model for Order Book Dynamics" [Operations Research 58(3)]
   - Poisson-based model for order arrivals, cancellations, and LOB dynamics
   - **Relevance**: Foundation for calibrating order arrival rates (lambda) on TMFD6 data, required for A-S parameter estimation

6. **Guo (2023)** -- "Market Making with Deep Reinforcement Learning from Limit Order Books" [arXiv:2305.15821]
   - RL-based market making with hard inventory limits
   - Shows that hard inventory constraints can be effectively managed through quote skewing
   - **Relevance**: Validates that hard inventory caps (like our 1-2 lot constraint) are compatible with profitable market making

### Expected IC Range

- **Composite edge**: Spread capture (Direction B) + reversal filtering (Direction A) + inventory skew (A-S theory) + phi_8min directional filter (R17)
- **Theoretical**: If spread gate provides +3 pts/trade and phi_8min improves win rate by 5%, net edge could be +1-4 pts/trade
- **Uncertainty**: VERY HIGH. This is a multi-component system where each component has its own uncertainty
- **Key unknown**: The interaction between components -- does phi_8min actually help when combined with spread gate, or are they redundant?

### Why Different from R12-R17 Failures

- Hard inventory cap (1-2 lots) prevents the catastrophic drawdown scenarios that killed R13's MM strategy (-30.6% DD)
- Explicit liquidation cost modeling prevents "trapped inventory" scenarios
- phi_8min filter (IC=0.041, R17) was identified but never used as a MM skew signal -- only tested as standalone taker
- Combines 3 separately validated components (spread gate, reversal signal, inventory control) into one framework

### Key Risks

1. **Complexity**: 4+ parameters to calibrate (gamma, spread threshold, reversal threshold, inventory cap, phi window). Overfitting risk is high.
2. **Component interaction uncertainty**: Each component was validated in isolation (different papers, different markets). Combined behavior is unknown.
3. **Latency**: A-S assumes instantaneous quote updates. 36-47ms broker RTT means ~100ms update cycle. During volatile periods, quotes become stale in that window.
4. **Market hours**: TMFD6 trades 08:45-13:45 (300 min). Strategy must handle opening auction, closing effects, and session boundaries.
5. **Execution**: Hard inventory cap + aggressive unwind means paying the full RT cost when hitting cap. If this happens frequently, it dominates profits.

### Data Requirements

- **L1 sufficient for base implementation**
- L2 helpful for microprice accuracy but not required
- Historical trade data for order arrival rate calibration
- phi_8min requires 8-minute lookback windows (already in FeatureEngine concept)
- **All available in ClickHouse**

---

## Cross-Cutting Analysis

### Novelty vs Prior Work (R12-R17)

| Aspect | R12-R17 Approach | R18 Approach |
|--------|-----------------|-------------|
| Order type | Market orders (taker) | Limit orders (maker) |
| Cost structure | Pay spread + fees = 4 pts | Earn spread - fees - adverse selection |
| Signal requirement | IC must overcome 1.33 bps (4 pts) | Signal only filters worst cases |
| Eligible time | 100% of trading time | 45.5% (spread >= 5 pts) |
| Instrument | TXFD6 primary | TMFD6 exclusively |
| Position sizing | Variable | Hard cap 1-2 lots |
| Prior test | Extensively backtested | Never tested on TMFD6 |

### What We Know vs What's Unknown

**Known (from data + prior rounds):**
- TMFD6 spread distribution: median 4, mean when >= 5: 19.7 pts
- TMFD6 tick rate: 1.8/sec
- TMFD6 queue depth: 4.1 lots at L1
- RT cost: 4 pts (40 NTD)
- No maker rebates for retail
- phi_8min: IC=0.041, orthogonal to OFI (R17)
- Broker RTT: place 36ms, modify 43ms, cancel 47ms (P95)

**Unknown (must be measured in Stage 2):**
- Adverse selection rate on TMFD6 during wide-spread periods
- Fill rate for limit orders at touch during wide-spread periods
- Order flow arrival rates (lambda_buy, lambda_sell) by spread regime
- Whether "reversals" (Albers' concept) exist with detectable frequency on TMFD6
- Queue position distribution for retail orders (are we always at back?)
- Correlation between spread regime duration and adverse selection

### Recommended Prioritization

1. **Direction B (SG-LP)** -- Simplest to validate. Just need to measure: (a) fill rate at touch when spread >= 5, (b) adverse selection rate conditional on spread. If adverse_rate < 60% at wide spreads, this alone is viable.

2. **Direction A (RCM)** -- Second priority. Requires building reversal classifier for TMFD6, but the Albers et al. framework provides a clear template. Can be tested independently on historical data.

3. **Direction C (IBH)** -- Synthesis of A+B. Only pursue if both A and B show independent promise. Too many parameters to be the first thing tested.

### Kill Criteria (Stage 2 exit conditions)

- **Kill B**: If adverse selection rate at spread >= 5 pts on TMFD6 > 70%, the spread gate is insufficient
- **Kill A**: If reversal frequency on TMFD6 < 10% (vs 15% on BTC perp), there's insufficient signal
- **Kill C**: If both A and B fail independently, C has no viable components to combine
- **Kill All**: If fill rate for limit orders at touch during spread >= 5 is < 30% (insufficient order flow)

---

## Appendix: Full Paper List

| # | Paper | Year | arXiv/Ref | Relevance |
|---|-------|------|-----------|-----------|
| 1 | Albers et al. -- "Market Maker's Dilemma" | 2025 | 2502.18625 | **Primary** -- Reversal model, fill-probability trade-off |
| 2 | Stoikov -- "The micro-price" | 2018 | SSRN:2970694 | **Primary** -- Fair value estimation |
| 3 | Gueant, Lehalle, F-T -- "Inventory Risk" | 2013 | 1105.3115 | **Primary** -- Closed-form MM with inventory constraints |
| 4 | Avellaneda & Stoikov -- "HFT in LOB" | 2008 | QF 8(3) | **Primary** -- Foundational MM framework |
| 5 | Cartea & Jaimungal -- "HF MM with inventory" | 2014 | 1206.4810 | **Core** -- Inventory constraints + directional bets |
| 6 | Cartea & Wang -- "MM with Alpha Signals" | 2020 | IJTAF 23(3) | **Core** -- Signal integration into MM |
| 7 | DeLise -- "Negative Drift of Limit Order Fill" | 2024 | 2407.16527 | **Core** -- Adverse selection quantification (bonds) |
| 8 | Gould & Bonart -- "Queue Imbalance as Predictor" | 2015 | 1512.03492 | **Core** -- OBI predictive power baseline |
| 9 | Blakely -- "Microprice with Tsetlin Machines" | 2024 | 2411.13594 | Supporting -- Enhanced microprice |
| 10 | Wang et al. -- "To Quote or Not to Quote" | 2023 | 2508.16588 | Supporting -- Selective quoting validation |
| 11 | Feldman & Maier-Paape -- "Adverse Selection + Price Reading" | 2025 | 2508.20225 | Supporting -- Inventory visibility risk |
| 12 | Zhang -- "Adaptive MM with Liquidation Cost" | 2024 | 2405.11444 | Supporting -- Liquidation cost modeling |
| 13 | Lokin & Yu -- "Fill Probabilities" | 2024 | 2403.02572 | Supporting -- Fill probability estimation |
| 14 | Cont, Stoikov, Talreja -- "Order Book Dynamics" | 2010 | OR 58(3) | Supporting -- LOB arrival rate model |
| 15 | Guo -- "MM with Deep RL from LOB" | 2023 | 2305.15821 | Supporting -- RL with inventory limits |
| 16 | Cartea, Donnelly, Jaimungal -- "Enhancing Trading" | 2018 | AMF 25(1) | Supporting -- OB signal features |
| 17 | Safari & Schmidhuber -- "Trends and Reversion" | 2025 | 2501.16772 | Context -- Multi-timescale regime analysis |
| 18 | Huang et al. -- "Queue-Reactive Model" | 2015 | 1312.0563 | Context -- Queue dynamics calibration |

---

## Verdict for Challenger Review

**Confidence level**: MODERATE. The academic foundations are strong (A-S framework is 18 years old and battle-tested). The novelty is in the *application context* (TMFD6's specific spread economics + retail constraints), not in the theory. The biggest unknown is whether TMFD6's wide-spread regime is benign (low adverse selection -- profitable) or loaded (high adverse selection -- losing). This is an empirical question that Stage 2 must answer.

**What the Challenger should scrutinize**:
1. The assumption that TMFD6 wide-spread periods are "benign" (not driven by informed flow)
2. Whether the Albers reversal result (BTC perpetual) transfers to TMFD6 (fundamentally different market)
3. The absence of maker rebates -- Albers' strategy relies on +1bp rebate per roundtrip
4. Whether 36ms RTT is fast enough for any maker strategy on TMFD6

# R47 Maker Pivot -- Stage 1 Researcher Report

**Date**: 2026-04-08
**Scope**: Literature survey, existing research mapping, candidate direction proposals
**Instrument context**: TXFD6 (200 NTD/pt, RT~5pts), TMFD6 (10 NTD/pt, RT~4pts)
**Latency**: System internal ~tens of us, Shioaji API RTT ~36ms
**Available features**: 27 real-time LOB features (FeatureEngine v3)

---

## 1. Literature Survey

### 1.1 Foundational Framework Papers

#### Gueant, Lehalle, Fernandez-Tapia (2011) -- "Dealing with the Inventory Risk" [1105.3115]
**Core idea**: Closed-form approximations for optimal bid/ask quotes under inventory constraints
within the Avellaneda-Stoikov framework. The market maker maximizes CARA utility, and optimal
quotes skew based on inventory level with an explicit risk-aversion parameter gamma.
**Key formula**: `delta_bid/ask = (1/gamma) * ln(1 + gamma/kappa) +/- (gamma * sigma^2 * (T-t) * q) / 2`
**Relevance**: HIGH. This is the canonical framework. Our `SimpleMarketMaker` already implements a
simplified version (imbalance + inventory skew). The full Gueant-Lehalle solution adds
time-dependent urgency and volatility-adjusted spread width.

#### Gueant (2016) -- "Optimal Market Making" [1605.01862]
**Core idea**: Generalizes all post-AS frameworks into a unified model. Proves existence of optimal
strategies. Extends to multi-asset MM. Provides closed-form approximations for multi-asset case.
**Relevance**: MEDIUM-HIGH. The multi-asset extension matters if we quote both TXFD6 and TMFD6
simultaneously (inventory correlation management).

#### Fodra & Labadie (2012) -- "HF Market-Making with Inventory Constraints and Directional Bets" [1206.4810]
**Core idea**: Extends AS/Gueant to NON-MARTINGALE mid-price processes (mean-reverting, trending).
Allows directional bets via asymmetric quoting while controlling inventory. Shows that with
mean-reverting mid-price, a market maker can increase PnL by 15% or increase Sharpe by 2x.
**Relevance**: CRITICAL. Our R44 VWAP MR signal (IC=-0.264 at 30min) is exactly a mean-reversion
predictor. This paper provides the theoretical foundation for using directional signals to skew
maker quotes rather than taking directional positions.

### 1.2 Adverse Selection and Toxicity Papers

#### Herdegen, Muhle-Karbe, Stebegg (2021) -- "Liquidity Provision with Adverse Selection and Inventory Costs" [2107.12094]
**Core idea**: Nash equilibrium model for dealers facing adverse selection vs. inventory costs.
Client types (informed vs. noise) are unknown; dealers choose price schedules to mitigate
between the two risks.
**Relevance**: MEDIUM. Theoretical backing for our Toxicity Score [21] -- high toxicity = informed
flow = widen quotes or pull.

#### Jafree, Jain, Firoozye (2025) -- "When AI Trading Agents Compete: Adverse Selection of Meta-Orders" [2510.27334]
**Core idea**: RL market maker learns to detect and profit from medium-frequency meta-orders using
Hawkes LOB model. Shows MMs can learn adverse selection patterns from order flow.
**Relevance**: MEDIUM. Validates that LOB features can detect adverse flow. Our 27-feature engine
already computes many of these signals (OFI, toxicity, depth momentum).

### 1.3 Queue Position and Fill Probability Papers

#### Lokin & Yu (2024) -- "Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows" [2403.02572]
**Core idea**: Semi-analytical expressions for fill probabilities at different price levels under
state-dependent order flows. Models LOB as interacting queuing systems. Validated on FX spot data.
**Relevance**: HIGH. Fill probability estimation is the single most important variable for maker
profitability at our latency. At 36ms RTT, we cannot compete on queue priority -- we must
estimate whether our resting order will fill AND whether the fill is profitable.

#### Bodor & Carlier (2024) -- "A Novel Approach to Queue-Reactive Models: The Importance of Order Sizes" [2405.18594]
**Core idea**: Extends queue-reactive model by incorporating order SIZES (not just arrival rates).
Calibrated on German bond futures. Shows volatility can be reproduced endogenously from order flow.
**Relevance**: HIGH. German bond futures are structurally similar to TAIFEX futures (discrete tick,
institutional flow). The order-size dimension is important because TAIFEX tick data shows 73%
qty=1 trades (R35 finding), meaning order size heterogeneity IS the signal.

### 1.4 Latency-Aware Market Making

#### Gao & Wang (2018) -- "Optimal Market Making in the Presence of Latency" [1806.05849]
**Core idea**: MDP formulation for large-tick market making with explicit latency. Shows profitability
requires sufficient uninformed flow relative to price jump rate. Latency is an additional risk
source that negatively impacts performance.
**Relevance**: CRITICAL. Our 36ms Shioaji RTT is the dominant constraint. This paper provides explicit
profitability criteria: positive expected profit if uninformed-order-arrival-rate / price-jump-rate
exceeds a threshold. We need to estimate this ratio for TAIFEX.

#### Jiang et al. (2025) -- "Resolving Latency and Inventory Risk in MM with RL (Relaver)" [2505.12465]
**Core idea**: RL-based MM with explicit 30-100ms latency modeling and batch auction mechanism.
Key innovations: (1) augmented state-action space with order hold time, (2) dynamic
programming-guided exploration, (3) market trend predictor for inventory management.
**Relevance**: CRITICAL. 30-100ms latency range matches our 36ms RTT exactly. The order hold time
concept is novel -- deciding how long to leave an order resting before canceling. The trend
predictor for inventory maps directly to our existing signals (QI_1, VWAP MR).

### 1.5 LOB-Feature-Driven and Adaptive MM

#### Lu & Abergel (2018) -- "Order-book Modelling and Market Making Strategies" [1806.05101]
**Core idea**: Non-Markovian LOB features dramatically improve MM performance vs. pure Markov models.
Identifies key statistical properties of order-driven markets that matter for practical MM.
**Relevance**: HIGH. Validates our approach of using LOB features rather than assuming simple Poisson
arrivals. Our 27-feature engine captures many non-Markovian features (EMA-based, autocovariance,
survival times).

#### Chavez-Casillas et al. (2024) -- "Adaptive Optimal MM with Inventory Liquidation Cost" [2405.11444]
**Core idea**: Closed-form optimal placement that adapts to real-time market order behavior. Models
partial fills with random-coefficient demand functions. No Brownian assumption needed.
**Relevance**: HIGH. The adaptive aspect is key -- adjusting quotes based on observed order flow in
real-time. This is precisely what our FeatureEngine enables.

### 1.6 Selective and Hawkes-Based MM

#### Wang, Ventre, Polukarov (2025) -- "Robust Market Making: To Quote, or not To Quote" [2508.16588]
**Core idea**: Enriches MM action space to include "refuse to quote" and "single-sided quote" options.
Shows that occasional quoting refusal improves returns AND Sharpe ratios.
**Relevance**: HIGH. This is exactly our OpportunisticMM concept (spread gate) extended with
RL-learned selective quoting.

#### Jusselin (2020) -- "Optimal Market Making with Persistent Order Flow" [2003.05958]
**Core idea**: Optimal MM with Hawkes-driven order flows capturing clustering and long memory.
**Relevance**: MEDIUM. We already have mm_hawkes.py implementing Hawkes-based MM with propagator.

---

## 2. Existing Research -> Maker Signal Mapping

| # | Existing Signal | Taker Role (KILLED) | Maker Role (NEW) | Paper Backing |
|---|---|---|---|---|
| 1 | R44 Night VWAP MR (IC=-0.264) | Predict direction, take position | Quote skew toward MR target (Fodra-Labadie directional) | 1206.4810 |
| 2 | TDA Beta-1 Takens (IC=+0.088) | Predict vol (no direction) | Spread width modulation (sigma in AS framework) | 1105.3115 |
| 3 | Toxicity [21] (FE approved) | N/A (filter signal) | Adverse selection gate: pull when toxicity > threshold | 2107.12094, 2508.16588 |
| 4 | QI_1 / L1 Imbalance [10] | Predict next tick (spread kills) | Queue priority estimator + asymmetric quote skew | 2403.02572, 2405.18594 |
| 5 | R40 Session pattern | Take momentum trades | Regime classifier for quoting intensity | 1806.05101 |
| 6 | OFI L1 (raw/cum/ema) [11-13] | Predict direction | Inventory skew driver | 2405.11444 |
| 7 | Return Autocovariance [17] | Predict reversal | Reversal quoting (neg autocov = MM paradise, tighten) | 1206.4810 |
| 8 | TOB Survival [18] | Predict instability | Fill risk indicator + requote interval optimizer | 1806.05849 |
| 9 | Depth-Norm OFI [16] | R16 candidate | Thin-book detector (pull when extreme + thin depth) | Takahashi |
| 10 | Deep Depth Momentum [20] | Predict L2-L5 shift | Anticipatory requoting toward supported side | Queue-reactive |
| 11 | Spread EMA 30s/300s [25,26] | N/A | Regime detection: wide=quote, tight=selective | OpportunisticMM |
| 12 | Multi-window OFI [22,23] | N/A | Multi-horizon flow: divergence=safe, alignment=pull | 2003.05958 |
| 13 | mm_hawkes.py | N/A | Ready backtest (Hawkes spread + propagator skew) | 2207.09951 |
| 14 | OpportunisticMM | N/A (CONCLUDED) | Spread-gate + reversal filter (needs layering) | 2508.16588 |
| 15 | SG-LP backtest | N/A | Backtest infrastructure for TMFD6 L1 data | N/A |

---

## 3. Candidate Directions

### Candidate A: Feature-Gated Avellaneda-Stoikov (FG-AS)

**One-line**: Classical AS/Gueant optimal MM with 27-feature engine driving parameters
(sigma, kappa, gamma) in real-time, plus spread-gate and toxicity-gate for regime filtering.

**Paper foundation**:
- Gueant, Lehalle, Fernandez-Tapia 2011 [1105.3115] -- Closed-form optimal quotes
- Fodra & Labadie 2012 [1206.4810] -- Directional bets within AS framework
- Gao & Wang 2018 [1806.05849] -- Latency-aware profitability criteria
- Wang et al. 2025 [2508.16588] -- Selective quoting (to-quote-or-not)

**Signal reuse**:
- Toxicity [21] -> adverse selection gate (pull quotes when toxicity > threshold)
- QI_1 / Imbalance [10] -> fill probability estimation + quote skew
- Return autocovariance [17] -> reversal regime detection (negative autocov = quote aggressively)
- Spread EMA 30s/300s [25,26] -> spread regime classifier (wide = quote, tight = pull)
- OFI L1 EMA [12,13] + multi-window [22,23] -> inventory skew direction
- R44 VWAP MR -> reservation price offset (skew toward MR target during night session)
- TDA Beta-1 -> sigma estimate for spread width modulation

**Code reuse**:
- `SimpleMarketMaker` -- base framework with imbalance skew + inventory management
- `OpportunisticMM` -- spread gate logic, reversal filter
- `SG-LP backtest` -- backtest infrastructure for TMFD6

**Implementation sketch**:
1. Compute reservation price: `r = mid + skew_ofi(OFI_ema) + skew_mr(VWAP_MR) - gamma*sigma^2*q*tau`
2. Compute optimal spread: `delta = (2/gamma)*ln(1 + gamma/kappa) + gamma*sigma_tda^2*tau`
3. Gate: Only quote if `spread_market > breakeven AND toxicity < threshold AND autocov < 0`
4. Set bid = r - delta/2, ask = r + delta/2
5. Requote every 100ms (3x RTT headroom)

**Key risk**:
- Parameter estimation: sigma, kappa, gamma must be calibrated to TAIFEX microstructure.
  Wrong calibration -> either too wide (no fills) or too tight (adverse fills).
- Fill rate at 36ms latency: We cannot reprice as fast as sub-ms HFTs. If informed traders
  pick us off before we can cancel, adverse selection dominates.

**Feasibility**: HIGH. Most conservative approach. All building blocks exist in code. AS framework
is well-understood with closed-form solutions. Risk is in calibration, not implementation.

---

### Candidate B: Latency-Aware Selective MM with Fill Probability Model (LSMM)

**One-line**: MM that explicitly models fill probability and adverse selection probability at
36ms latency, only quoting when expected profit per quote is positive.

**Paper foundation**:
- Gao & Wang 2018 [1806.05849] -- Profitability criteria with latency
- Lokin & Yu 2024 [2403.02572] -- Fill probability estimation from LOB state
- Jiang et al. 2025 [2505.12465] -- Relaver: latency-aware RL MM with 30-100ms delays
- Bodor & Carlier 2024 [2405.18594] -- Queue-reactive model with order sizes

**Signal reuse**:
- L1 bid/ask qty [8,9] -> queue depth, fill probability estimate
- TOB Survival [18] -> price stability indicator, fill window estimate
- Toxicity [21] -> P(adverse fill) estimation
- Deep Depth Momentum [20] -> anticipate queue depletion
- Depth-Norm OFI [16] -> thin-book adverse selection risk

**Core innovation**:
Instead of always quoting, compute for each potential quote:
- `P_fill(delta, queue_depth, tob_survival)` = probability order fills before we can cancel
- `P_adverse(toxicity, ofi_flow, depth_norm_ofi)` = probability fill is adversely selected
- `E[profit] = P_fill * [(1-P_adverse) * half_spread - P_adverse * adverse_move] - fee`
- Only quote when `E[profit] > 0`

**Code reuse**:
- `OpportunisticMM` -- spread gate + selective quoting framework
- `mm_hawkes.py` -- Hawkes intensity for arrival rate estimation (kappa proxy)
- Feature engine [8,9,16,18,20,21] -- real-time inputs for probability model

**Key risk**:
- Model accuracy: Fill/adverse probability estimates need calibration on historical data.
- Quote frequency: If too conservative, fixed costs dominate. Need minimum fill rate.
- Data requirement: Need fill probability model from replay data (6.27M+ ticks available).

**Feasibility**: MEDIUM-HIGH. Directly addresses latency handicap. Requires empirical work
(fill probability calibration via hftbacktest replay). Build on Candidate A infrastructure.

---

### Candidate C: Multi-Regime Conditional MM (MRCMM)

**One-line**: State-machine MM classifying into 3-4 regimes using feature engine, applying
different quoting strategy per regime.

**Paper foundation**:
- Lu & Abergel 2018 [1806.05101] -- Non-Markovian features improve MM
- Fodra & Labadie 2012 [1206.4810] -- Directional bets in mean-reversion regime
- Wang et al. 2025 [2508.16588] -- Selective quoting with refusal option
- Chavez-Casillas et al. 2024 [2405.11444] -- Adaptive placement to market order behavior

**Signal reuse**:
- Return autocovariance [17] -> REGIME: negative=reverting, positive=trending
- Spread EMA 300s [26] vs current spread -> REGIME: wide vs. tight
- Toxicity [21] -> REGIME: informed vs. noise-dominated flow
- R40 session pattern -> REGIME: time-of-day effects
- OFI multi-window divergence [12 vs 23] -> REGIME: transient vs. persistent imbalance

**Regime definitions**:

| Regime | Conditions | Quoting Strategy | Expected Edge |
|--------|-----------|-----------------|---------------|
| CALM | Low toxicity, neg autocov, spread>=breakeven | Two-sided tight quotes | Spread capture - fee |
| WIDE | Spread >> breakeven, any toxicity | Two-sided wider quotes | Large spread capture |
| TRENDING | Pos autocov, sustained OFI, high toxicity | Single-sided or pull | Capital preservation |
| HALT | Extreme toxicity, feed gap, StormGuard | No quotes | Capital preservation |

**Code reuse**:
- `OpportunisticMM` -- reversal filter + spread gate = CALM+WIDE subset
- `SimpleMarketMaker` -- base quoting logic
- StormGuard FSM -- HALT detection
- Feature engine [17,21,25,26,22,23] -- all regime signals live

**Key risk**:
- Regime misclassification: TRENDING as CALM = adverse fills. CALM as TRENDING = missed profits.
- Insufficient CALM time: If market trends/halts most of session, too few quoting windows.
- Complexity: 4 regimes = more parameters to calibrate. Overfitting risk.

**Feasibility**: MEDIUM. Most aligned with our signal portfolio. Evolution path: start with A,
layer on regime logic as we collect live MM data.

---

## 4. Prioritization

| Priority | Candidate | Rationale |
|----------|-----------|-----------|
| 1st | A (FG-AS) | Lowest implementation risk. All code exists. Closed-form. Focus on calibration. |
| 2nd | B (LSMM) | Addresses latency directly. Requires fill probability model. Build on A. |
| 3rd | C (MRCMM) | Evolution path from A. Layer regime logic with live fill data. |

**Critical pre-work for ALL candidates**:
1. Estimate uninformed/informed flow ratio on TAIFEX using toxicity signal distribution
2. Compute empirical fill probability from hftbacktest replay at different queue depths
3. Validate spread > breakeven fraction (OpMM killed: March spread=3pts < RT cost=3.92pts)

**Structural advantage of maker pivot**:
Maker earns spread instead of paying it. At breakeven spread of 4pts on TMFD6, a maker who
captures spread with 0 adverse selection nets ~2pts/RT after fees. At 50 fills/day = 500 NTD/day.
Challenge: achieving 50 fills/day with positive expectation at 36ms latency.

---

## Appendix: Key arXiv References

| ID | Short Title | Core Contribution |
|----|-------------|-------------------|
| 1105.3115 | Gueant-Lehalle-Tapia "Inventory" | Closed-form AS with inventory constraints |
| 1605.01862 | Gueant "Optimal MM" | Unified framework, multi-asset extension |
| 1206.4810 | Fodra-Labadie "Directional Bets" | Non-martingale MM, mean-reversion skew |
| 1806.05849 | Gao-Wang "Latency" | Profitability criteria with latency |
| 2505.12465 | Jiang+ "Relaver" | RL MM with 30-100ms latency |
| 2403.02572 | Lokin-Yu "Fill Probabilities" | State-dependent fill probability model |
| 2405.18594 | Bodor-Carlier "Queue-Reactive" | Order-size-aware queue model (bond futures) |
| 1806.05101 | Lu-Abergel "Order-book MM" | Non-Markovian features improve MM |
| 2405.11444 | Chavez-Casillas+ "Adaptive MM" | Closed-form adaptive placement |
| 2508.16588 | Wang+ "To Quote or Not" | Selective quoting improves Sharpe |
| 2107.12094 | Herdegen+ "Adverse Selection" | Nash equilibrium with adverse selection |
| 2003.05958 | Jusselin "Persistent Order Flow" | Hawkes-driven optimal MM |
| 2510.27334 | Jafree+ "AI Agents Compete" | RL MM learns adverse selection |
| 2207.09951 | Gasperov+ "DRL Hawkes MM" | RL + Hawkes LOB simulator |
| 2306.02764 | Gong+ "Chinese Stock MM" | AS in Chinese market with stamp duty |

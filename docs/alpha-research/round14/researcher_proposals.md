# Round 14 Alpha Research: Literature Survey & Candidate Proposals

**Researcher**: researcher
**Date**: 2026-03-25
**Survey Scope**: arXiv q-fin.TR / q-fin.ST / q-fin.MF, 2022-2026

## Survey Summary

Searched 8 topic areas across ~80 papers. Key themes in recent microstructure research:
- Order-flow entropy as volatility state variable (Singha 2025)
- Core/reaction flow decomposition via Hawkes processes (Muhle-Karbe, Rosenbaum et al. 2026)
- Multi-scale posting distance as price formation driver (Fabre & Challet 2025)
- LOB physics analogies: kinetic energy, active depth (Li et al. 2023)
- Queue-reactive models with order-size dependence (Bodor & Carlier 2024/2025)
- Portable cross-asset microstructure features (Bieganowski & Slepaczuk 2026)

## Existing Platform Coverage (DO NOT DUPLICATE)

| Domain | Existing Implementations |
|--------|------------------------|
| OFI variants | ofi_depth_divergence, ofi_entropy, ofi_filtered_l2, ofi_pinning, ofi_depth_elastic, ofi_futures_spot_leadlag, ofi_surprise |
| VPIN | vpin_regime_switch |
| Entropy | entropy_toxicity |
| MLOFI | mlofi_gradient (IC=-0.105, Gate C FAIL) |
| Impact | impact_surprise |
| Depth | mldm_depth_momentum, book_convexity |
| FeatureEngine v2 | 17 features (8 stateless + 8 rolling + mlofi_gradient_x1000) |

---

## Candidate 1: `orderflow_markov_entropy`

### Paper Reference
- **arXiv ID**: 2512.15720v1
- **Title**: "Hidden Order in Trades Predicts the Size of Price Moves"
- **Authors**: Mainak Singha (NASA/CUA)
- **Published**: December 2025

### Core Signal
Real-time order-flow entropy computed from a Markov transition matrix over trade states. Each second, the market is classified into one of 15 states: {price_sign: -1,0,+1} x {volume_quintile: 1..5}. A transition matrix is estimated over a rolling 120-second window, and normalized Shannon entropy is computed:

```
H_t = -1/log(K) * sum_i pi_i * sum_j p_ij * log(p_ij)
```

where K=15, pi is the stationary distribution, p_ij are transition probabilities.

**Key insight**: Low entropy detects informed trading activity (persistent order patterns) WITHOUT revealing direction. Entropy < 5th percentile predicts 2.89x larger absolute 5-min returns (t=12.41, p<1e-4). Directional accuracy = 45% (indistinguishable from chance).

### Expected Signal Type
**Volatility regime** -- predicts magnitude of upcoming moves, not direction. Use as:
1. Volatility timing overlay for existing directional alphas
2. Dynamic position sizing (larger when entropy signals big moves)
3. Straddle-like entry with tight stops (asymmetric payoff)

### Half-life Estimate
Signal measured at second resolution; predictive for 5-minute horizon. Half-life ~2-5 minutes. Comfortably within 36ms RTT constraints (signal changes slowly relative to execution speed).

### Feasibility Assessment
**STRONG FEASIBILITY**
- Requires only trade data (price + volume at tick level) -- already available via Shioaji
- Computation: 120-second rolling window, 15x15 matrix update + eigendecomposition -- trivially fast, can be done in Python or Rust
- No LOB depth data needed (trade-only signal)
- 36ms RTT is fine: signal changes on ~second timescale, trades are at ~125ms intervals for TXFD6
- Fee concern: The paper's SPY backtest used 0.57 bps round-trip. TAIFEX retail fees are higher (~2.0 bps sell tax + commissions). However, the signal predicts 2.89x magnitude amplification. If unconditional abs return is ~5 bps per 5 min, conditioned moves are ~15 bps -- sufficient headroom above fees.

### Novelty vs Existing
- **entropy_toxicity**: Measures Shannon entropy of trade flow signs only. `orderflow_markov_entropy` uses a 15-state Markov chain with volume quintiles AND transition structure, which is fundamentally richer.
- **VPIN**: Measures volume-clock informed trading probability. Entropy captures a different aspect: temporal predictability of state sequences, not just buy/sell volume imbalance.
- **No overlap** with any OFI variant (those measure directional flow imbalance; entropy is direction-invariant by construction).

### Risk Factors
1. Paper validated on only 36 days of SPY data -- needs TXFD6/TWSE validation
2. TXFD6 has 3.7 ticks/sec vs SPY's ~1000/sec -- state transitions may be too sparse for 120-sec windows. May need longer windows (300-600 sec).
3. 38.5% of paper's profits came from one day (Oct 29) -- concentration risk
4. Without directional signal, must pair with existing alpha or use symmetric payoff structure

---

## Candidate 2: `lob_kinetic_energy`

### Paper Reference
- **arXiv ID**: 2308.14235v6
- **Title**: "An Empirical Analysis on Financial Markets: Insights from the Application of Statistical Physics"
- **Authors**: Haochen Li, Yi Cao, Maria Polukarov, Carmine Ventre
- **Published**: August 2023 (v6 updated)

### Core Signal
Treats LOB orders as particles in a physical system, computing "kinetic energy" and "momentum" of the book:

**Kinetic Energy** (per side):
```
KE_bid = 0.5 * sum_i q_i * v_i^2
KE_ask = 0.5 * sum_i q_i * v_i^2
```
where q_i = quantity at level i, v_i = rate of change of quantity at level i (computed over rolling window).

**LOB Momentum** (directional):
```
P = sum_i q_i * v_i  (bid side) - sum_i q_i * v_i  (ask side)
```

**Active Depth**: Identifies the deepest book level that still has statistically significant impact on price dynamics, computed via correlation analysis. Orders beyond active depth are "noise" -- filtered out.

The key innovation is `active_depth`: instead of using all 5 (or N) levels, only levels within the "active depth" boundary contribute to energy/momentum calculations. This dramatically improves signal-to-noise.

### Expected Signal Type
**Momentum / mean-reversion hybrid**:
- High |momentum| (asymmetric energy) predicts directional moves (momentum)
- Energy spikes without directional bias predict volatility expansion (similar to entropy signal but from LOB side)
- Energy dissipation after spikes predicts mean reversion

### Half-life Estimate
Tick-to-tick signal; predictive horizon 1-30 seconds. Fast-decaying. At 125ms tick intervals, this operates comfortably within signal timescales.

### Feasibility Assessment
**GOOD FEASIBILITY**
- Requires 5-level LOB snapshots -- already available from `BidAskEvent` with shape (N,2) = (price, qty) per side
- Computation: differences of quantities across consecutive snapshots, weighted sums -- O(N) per tick, very fast
- Active depth calculation can be done offline (calibrated daily) then applied in real-time
- 36ms RTT: Signal updates at tick speed (~125ms). Execution latency is ~28% of tick interval -- tight but workable for directional signals with >1 tick holding period

### Novelty vs Existing
- **book_convexity**: Measures static curvature of LOB shape. `lob_kinetic_energy` measures the *dynamics* (rate of change) of book quantities -- fundamentally different.
- **mldm_depth_momentum**: Measures depth changes at individual levels. KE/momentum is a *weighted aggregate* with the physics-inspired q*v^2 formulation and active-depth filtering.
- **OFI variants**: Measure flow at best bid/ask. KE uses multi-level rate-of-change with quantity weighting.
- **mlofi_gradient**: Measures spatial gradient across levels. KE measures temporal dynamics within levels.

### Risk Factors
1. Paper used L3 data (individual orders); we only have L2 (5-level aggregated). Active depth concept may be less powerful with only 5 levels.
2. v_i (rate of change of quantity) is noisy at tick resolution -- may need smoothing
3. TXFD6 with 5 levels may not have enough depth variation for meaningful energy calculations
4. Paper validated on cryptocurrency data -- TWSE microstructure differs significantly

---

## Candidate 3: `hawkes_flow_imbalance`

### Paper References
- **Primary**: arXiv 2601.23172v2 -- "A unified theory of order flow, market impact, and volatility" (Muhle-Karbe, Rosenbaum, et al., January 2026)
- **Supporting**: arXiv 2504.15908v1 -- "Learning the Spoofability of Limit Order Books" (Fabre & Challet, April 2025)
- **Supporting**: arXiv 2405.18594v1 -- "A Novel Approach to Queue-Reactive Models" (Bodor & Carlier, May 2024)

### Core Signal
Decompose observed order flow into **core flow** (initiating, information-bearing) and **reaction flow** (mechanical responses to price changes), then use the core-flow intensity as a predictive signal.

Practical implementation (simplified from the Hawkes framework):

```
# For each trade at time t:
lambda_core(t) = mu + sum_{t_i < t} alpha_core * exp(-beta_core * (t - t_i)) * I(t_i is core)
lambda_react(t) = sum_{t_i < t} alpha_react * exp(-beta_react * (t - t_i)) * I(t_i is react)

# Classification heuristic (from Fabre & Challet):
# A trade is "core" if it arrives outside the expected reaction window
# A trade is "reaction" if it arrives within tau_react of a prior price move

# Signal:
core_flow_imbalance = signed_core_intensity_buy - signed_core_intensity_sell
```

The key insight from Muhle-Karbe et al.: the persistence parameter H_0 of core flow (~3/4) determines all key market properties. When core flow is unusually persistent (H_0 temporarily elevated), large price impact follows. When reaction flow dominates, price impact is transient.

**Simplified implementable version**: Use exponentially-weighted signed trade flow, but weight trades by their "surprise" factor -- how much they deviate from the expected reaction pattern. Trades that arrive at unexpected times or with unexpected sizes get higher weight.

```
surprise_weight(t_i) = 1 / max(lambda_react(t_i), epsilon)
core_weighted_ofi = sum_i sign_i * volume_i * surprise_weight(t_i) * exp(-decay * (t - t_i))
```

### Expected Signal Type
**Momentum** -- core flow persistence predicts continued directional price movement. When informed traders are active (high core flow), prices trend in their direction.

### Half-life Estimate
Core flow persistence H_0 ~ 3/4 implies long memory. Signal half-life: 30 seconds to several minutes. This is the slowest-decaying candidate -- well suited for 36ms RTT.

### Feasibility Assessment
**MODERATE FEASIBILITY**
- Requires trade-level data with timestamps -- available from Shioaji tick callbacks
- Core/reaction classification needs calibrated Hawkes parameters (mu, alpha, beta) -- can be estimated offline daily
- Simplified "surprise-weighted OFI" version avoids full Hawkes estimation at runtime
- 36ms RTT: Signal evolves on 30s+ timescale -- very comfortable
- Fee structure: If core flow genuinely identifies informed traders, the signal should predict moves large enough to cover fees. The paper shows square-root impact law, so larger core flow => proportionally larger moves.

### Novelty vs Existing
- **OFI variants**: All treat trades equally. `hawkes_flow_imbalance` weights trades by how "surprising" they are relative to the expected mechanical reaction pattern.
- **entropy_toxicity**: Measures aggregate randomness. Hawkes decomposition identifies the *source* of non-randomness (core vs reaction).
- **impact_surprise**: Measures price impact deviation from expected. `hawkes_flow_imbalance` works on the *flow side* (which trades are informative), not the price side.
- **core_reaction_flow_ratio**: Related but distinct -- the existing alpha computes a ratio of flow types. This candidate uses the Hawkes kernel to properly time-weight and decompose, and produces a directional imbalance signal rather than a ratio.

### Risk Factors
1. Full Hawkes calibration is computationally expensive -- simplified version loses theoretical elegance
2. TXFD6 at 3.7 ticks/sec may not have enough events for reliable Hawkes estimation
3. Core/reaction classification is inherently noisy -- misclassification dilutes signal
4. The theoretical framework assumes continuous trading; TWSE has discrete sessions with gaps
5. `core_reaction_flow_ratio` already exists -- need to demonstrate significant improvement from Hawkes-weighted version

---

## Recommendation Ranking

| Rank | Candidate | Confidence | Novelty | Feasibility | Fee Robustness |
|------|-----------|------------|---------|-------------|----------------|
| 1 | `orderflow_markov_entropy` | HIGH | HIGH | HIGH | MODERATE |
| 2 | `lob_kinetic_energy` | MODERATE | HIGH | GOOD | MODERATE |
| 3 | `hawkes_flow_imbalance` | MODERATE | MODERATE | MODERATE | GOOD |

### Rationale for Ranking

**#1 `orderflow_markov_entropy`** is the strongest candidate because:
- Clear theoretical foundation (Shannon entropy + Kyle/Glosten-Milgrom)
- Strong empirical results with proper walk-forward validation
- Implementation is simple and computationally cheap
- Uses trade-only data (no LOB depth needed)
- Novel approach not covered by any existing alpha
- Natural fit as volatility overlay for existing directional signals

**#2 `lob_kinetic_energy`** is promising because:
- Physics-inspired approach is genuinely novel for this platform
- Uses existing LOB data in a fundamentally different way (dynamics vs statics)
- Active depth concept could improve many existing features
- But: limited to 5-level data may reduce effectiveness

**#3 `hawkes_flow_imbalance`** is worth exploring because:
- Best theoretical foundation (Rosenbaum group)
- Longest signal half-life (most RTT-friendly)
- But: overlap risk with existing `core_reaction_flow_ratio`
- Hawkes calibration complexity is a practical concern

## Key Papers Reviewed

| arXiv ID | Title | Relevance |
|----------|-------|-----------|
| 2512.15720v1 | Hidden Order in Trades Predicts the Size of Price Moves | **PRIMARY** - Candidate 1 |
| 2308.14235v6 | Empirical Analysis: Statistical Physics on Financial Markets | **PRIMARY** - Candidate 2 |
| 2601.23172v2 | Unified theory of order flow, market impact, and volatility | **PRIMARY** - Candidate 3 |
| 2504.15908v1 | Learning the Spoofability of LOBs | **SUPPORTING** - posting distance insight |
| 2405.18594v1 | Queue-Reactive Models: Importance of Order Sizes | **SUPPORTING** - order size in QR models |
| 2602.00776v1 | Explainable Patterns in Cryptocurrency Microstructure | **CONTEXT** - portable features |
| 2501.08822v1 | Deep Learning Meets Queue-Reactive | **CONTEXT** - MDQR architecture |
| 2508.06788v4 | Returns and Order Flow Imbalances: Intraday Dynamics | **CONTEXT** - structural VAR at 1-sec |

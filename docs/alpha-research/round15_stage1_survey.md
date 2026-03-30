# Round 15 — Stage 1 Literature Survey

**Date**: 2026-03-25
**Researcher**: Claude (AI Agent)
**Status**: Stage 1 Complete — 3 Candidate Directions Proposed

---

## 1. Research Context & Constraints

### What we have
- TXFD6 L5 LOB: 2.17M ticks, 11 days; L1: 6.3M rows, 12 days; L2 hftbacktest: 16.6M events, 4 days
- FeatureEngine v2: 18 features (OFI, EMA spread/imbalance, ISS, MLDM, mlofi_gradient, etc.)
- DriftBurstDetector (StormGuard integrated), VPIN regime detector (platform-ready)
- TickEvent fields: price (x10000), volume, meta (timestamps). **No trade-side classification.**
- BidAskEvent fields: bids/asks arrays (N,2) up to L5 depth, stats tuple

### What failed structurally (Rounds 12-14)
- **Standalone taker alphas on TXFD6**: spread (79 pts) >> signal edge (1-5 pts). IC ~0.01-0.05 cannot overcome costs
- **Bidirectional MM at 36ms RTT**: Queue-back adverse selection. Sharpe -100 to -244
- **Per-day IC averaging**: Inflates results. Must use pooled IC
- **Walk-forward collapse**: P2-lite IS Sharpe +3.80 → OOS -9.8 to -25.7

### Hard constraints
- Shioaji P95 latency: submit=36ms, modify=43ms, cancel=47ms
- TAIFEX: ~125ms median inter-tick, no maker rebates, full 2.0 bps sell tax
- No trade-side (buy/sell) classification in TickEvent
- All prices scaled int x10000

### Recommended next directions (from Round 13 report)
- P0: MXFD6 MM (lower liquidity, less queue competition)
- P1: Conditional P2-lite (tight-spread regime only)
- P2: Cross-product lead-lag (TXFD6 vs MXFD6)
- P3: Latency reduction (infrastructure)
- P4: Event-driven trading (drift bursts / regime transitions)

---

## 2. Literature Search Summary

### Search queries executed (arXiv MCP)
1. `"limit order book" AND ("lead-lag" OR "cross-asset" OR "price discovery")` — q-fin.TR/ST/MF
2. `"order flow" AND ("regime" OR "conditional" OR "event-driven") AND ("futures" OR "high-frequency")` — q-fin.TR/ST
3. `"order book imbalance" AND ("prediction" OR "alpha" OR "signal") AND "high-frequency"` — q-fin.TR/ST/MF
4. `"LOB" AND ("deep learning" OR "neural network") AND ("mid-price" OR "return prediction")` — q-fin.TR/ST, cs.LG
5. `"cross-asset" AND ("lead-lag" OR "price discovery") AND ("futures" OR "index")` — q-fin.TR/ST/MF
6. `"order book" AND ("depth" OR "shape" OR "volume imbalance") AND ("predictive" OR "forecasting")` — q-fin.TR/ST
7. `"event-driven" AND ("microstructure" OR "order book") AND ("conditional" OR "regime")` — q-fin.TR/ST/MF
8. `"liquidity withdrawal" OR "liquidity shock" AND ("order book" OR "LOB")` — q-fin.TR/ST

### Papers read in full
- **2509.22985** — Wang (2025), "Forecasting Liquidity Withdrawal with ML Models"
- **2508.06788** — Takahashi (2025), "Returns and Order Flow Imbalances: Intraday Dynamics"

### Papers analyzed from abstracts (download pending)
- **2602.00776** — Bieganowski & Slepaczuk (2026), "Explainable Patterns in Cryptocurrency Microstructure"
- **2308.14235** — Li et al. (2023), "Empirical Analysis on Financial Markets: Statistical Physics LOB"
- **2507.05749** — Anantha et al. (2025), "Event-Time Anchor Selection for Multi-Contract Quoting"
- **2507.22712** — Anantha et al. (2025), "Order-Flow Filtration and Directional Association"
- **2505.17388** — Hu & Zhang (2025), "Stochastic Price Dynamics in Response to OFI"
- **2603.20456** — Hu (2026), "Neural HMM with Adaptive Granularity for HF Order Flow"
- **2601.23172** — Muhle-Karbe et al. (2026), "Unified Theory of Order Flow, Market Impact, and Volatility"
- **2407.16527** — DeLise (2024), "The Negative Drift of a Limit Order Fill"

---

## 3. Candidate Alpha Directions

### Candidate A: Liquidity Withdrawal Anticipation (LWA)

**Paper references**: 2509.22985 (Wang 2025), 2308.14235 (Li et al. 2023)

**Signal description**:
Construct a Liquidity Withdrawal Index (LWI) adapted for TXFD6 LOB data. The original LWI = cancellations / (standing_depth + new_additions) at L1. We extend this using L5 depth data to build a multi-level withdrawal signal:
- Track cancel-rate vs add-rate ratios at each of L1-L5
- Detect asymmetric withdrawal (bid-side vs ask-side independently)
- Use rolling z-score of LWI to detect abnormal withdrawal episodes

**Intuition**: Before significant price moves, informed traders and fast market makers withdraw liquidity from the book. This withdrawal is detectable 1-5 seconds before price impact arrives. Rather than predicting price direction directly (which requires overcoming the spread), we predict *when the book is about to become fragile* and use this as a conditional gate for existing signals (ISS, MLDM, OFI).

**Why this avoids past failures**:
- NOT a standalone taker alpha — it's a **conditional gate** that amplifies existing features
- Works with our data: only needs BidAskEvent bids/asks arrays (L1-L5 depth), no trade-side needed
- Event-driven: only trades when LWI spikes, dramatically reducing trade frequency and cost exposure
- Aligned with P4 recommendation (event-driven trading on regime transitions)

**Expected data requirements**:
- BidAskEvent: bids/asks arrays (N,2) — compute depth changes between snapshots
- TickEvent: price, volume — for validation of withdrawal → price move relationship
- Need to reconstruct add/cancel from consecutive BidAskEvent snapshots (diff-based)

**Estimated IC range**: 0.02-0.08 for LWI predicting abs(return), based on Wang (2025) R² ~0.68-0.93 at 1-5s horizons for similar features. IC for *conditional* directional signal (LWI × OFI) could be higher.

**Implementation complexity**: **Low-Medium**
- Core LWI: depth diffs between consecutive BidAskEvents (~50 lines Python)
- Multi-level extension: iterate over L1-L5 (~100 lines)
- Conditional gate integration with FeatureEngine: straightforward (new feature index 18+)
- No ML model needed for v1 — pure microstructure signal

**Relationship to existing features**:
- **Complementary** to OFI/ISS/MLDM: LWI measures *withdrawal* (supply reduction), while OFI measures *flow* (demand pressure). Theoretically near-orthogonal
- **Enhances** VPIN regime detector: LWI spikes should correlate with VPIN transitions
- **Extends** DriftBurstDetector: withdrawal often precedes drift bursts

**Key risk**: On TXFD6 with 125ms tick intervals, the "anticipation window" may be too short for 36ms latency to act on. Must validate that LWI elevation persists for multiple ticks before price impact.

---

### Candidate B: Regime-Conditional OFI with Horizon-Dependent Dynamics

**Paper references**: 2508.06788 (Takahashi 2025), 2505.17388 (Hu & Zhang 2025), 2507.22712 (Anantha et al. 2025)

**Signal description**:
The OFI-to-price relationship is not constant — it varies systematically with:
1. **Depth regime**: When depth is thin, OFI has 2-5x higher price impact (Takahashi: b_r ∝ 1/2D)
2. **Spread regime**: Tight spread = high information content; wide spread = noise
3. **Volatility regime**: OFI predictiveness peaks in moderate volatility (Hu & Zhang: regime-dependent memory)

Build a regime-conditional OFI signal:
- Classify current state into (depth_regime × spread_regime × vol_regime) = up to 8 bins
- Apply regime-specific OFI scaling factors (calibrated from historical data)
- Only generate OrderIntents when the regime is favorable (high depth sensitivity, tight spread)

This is essentially the **P1 recommendation** (Conditional P2-lite) but grounded in proper microstructure theory.

**Intuition**: Our existing OFI features have IC ~0.01-0.05 unconditionally. But the Takahashi (2025) SVAR results show that the structural price impact parameter b_r varies by 10x across intraday regimes (0.0 to 5.0). If we condition on regimes where b_r is high (thin depth, normal spread), the *conditional IC* should be substantially higher. The key insight from Anantha et al. (2025) is that filtering order flow (removing transient/flickering orders) further sharpens the OBI-return association.

**Why this avoids past failures**:
- NOT unconditional trading — only trades in favorable regimes (addressing P2-lite OOS collapse)
- Uses existing features (OFI, spread, depth, imbalance) — no new data requirements
- Regime classification uses observable state variables, not fitted parameters
- The "filtration" concept (Anantha 2025) can be approximated by requiring consecutive BidAsk confirmations

**Expected data requirements**:
- BidAskEvent: bids/asks for OFI computation (already in FeatureEngine)
- Depth at L1 (already available), spread (already available)
- Rolling volatility (already in FeatureEngine: EMA spread, imbalance)
- No new fields needed

**Estimated IC range**:
- Unconditional OFI IC: ~0.01-0.05 (known from Rounds 12-14)
- Conditional IC in favorable regime: ~0.05-0.15 (based on 2-5x amplification from Takahashi depth scaling)
- Net tradable edge after costs: uncertain — depends on regime frequency and trade count

**Implementation complexity**: **Low**
- Regime classifier: threshold-based on depth/spread/vol — ~30 lines
- Conditional scaling: lookup table — ~20 lines
- Integration: wrap existing OFI feature with regime gate in FeatureEngine

**Relationship to existing features**:
- **Direct enhancement** of existing OFI features (indices 0-7 in FeatureEngine)
- **Complementary** to VPIN regime detector: uses different regime dimensions (depth/spread vs toxicity)
- **Extends** ISS concept: ISS measures OFI sensitivity adaptively; this makes the conditioning explicit

**Key risk**: Regime frequency — if the "favorable" regime only occurs 5-10% of the time, there may not be enough trades for statistical significance. Also, the Takahashi results are from E-mini S&P 500 (much more liquid than TXFD6); the regime dynamics may differ.

---

### Candidate C: LOB Active Depth Momentum (Statistical Physics Approach)

**Paper references**: 2308.14235 (Li et al. 2023), 2602.00776 (Bieganowski & Slepaczuk 2026)

**Signal description**:
Inspired by the statistical physics framework of Li et al. (2023), which treats the LOB as a particle system:
- **Kinetic Energy (KE)**: Sum of (depth × price_distance²) across levels — measures potential energy stored in the book
- **Momentum (P)**: Asymmetry of KE between bid and ask sides — directional pressure from deep levels
- **Active Depth**: Dynamically identify which LOB levels actually impact price dynamics (not necessarily L1)

Concretely for TXFD6 L5:
```
KE_bid = Σ_{i=1}^{5} bid_qty[i] × (mid - bid_price[i])²
KE_ask = Σ_{i=1}^{5} ask_qty[i] × (ask_price[i] - mid)²
LOB_momentum = (KE_bid - KE_ask) / (KE_bid + KE_ask)
```

This extends our existing MLDM feature (which only tracks depth *changes* at L2-L5) by incorporating the *spatial distribution* of depth relative to mid-price.

**Intuition**: Large orders parked at deeper levels represent latent supply/demand that constrains future price movement. The "momentum" captures whether the book's gravitational center is pulling price up (more ask-side weight = resistance above) or down (more bid-side weight = support below). Li et al. report outperformance over traditional OBI for volatility and return prediction.

**Why this avoids past failures**:
- Uses full L5 depth data (exploiting data advantage we already have)
- Physics-based features are inherently different from flow-based signals (OFI/ISS) — true orthogonality
- Can serve as both a standalone feature and a regime classifier for Candidate B
- Bieganowski & Slepaczuk (2026) demonstrate cross-asset stability of similar LOB shape features

**Expected data requirements**:
- BidAskEvent: full bids/asks arrays (N,2) for L1-L5 — already available
- price and quantity at each level — directly from bids/asks arrays
- No new fields needed

**Estimated IC range**: 0.02-0.06 based on Li et al. results (outperforms traditional approaches). The "active depth" concept may provide additional lift by focusing on levels that actually correlate with price changes on TXFD6.

**Implementation complexity**: **Low**
- KE/Momentum computation: vectorized numpy over bids/asks arrays — ~30 lines
- Active depth detection: rolling correlation of level-specific depth changes with price — ~50 lines
- FeatureEngine integration: new feature indices 18-20 (lob_ke_momentum, lob_active_depth_ratio, lob_gravity_center)

**Relationship to existing features**:
- **Orthogonal** to OFI (flow-based) and ISS (sensitivity-based): measures *static spatial structure* of the book
- **Extends** MLDM: MLDM captures depth withdrawal dynamics; LOB momentum captures depth *distribution* state
- **Complementary** to spread features: spread measures L1 gap; KE measures full L1-L5 weight distribution

**Key risk**: The "active depth" finding in Li et al. uses L3 (full order-level) data with individual order tracking. Our L5 aggregate depth snapshots are coarser — the signal may be weaker. Also, the original results are on equities, not futures.

---

## 4. Candidate Comparison Matrix

| Dimension | A: LWA | B: Regime-Conditional OFI | C: LOB Active Depth |
|-----------|--------|--------------------------|---------------------|
| **Paper grounding** | Strong (Wang 2025, R²>0.90 at 5s) | Strong (Takahashi 2025 SVAR, 6yr E-mini) | Moderate (Li 2023, equities L3) |
| **Data feasibility** | Good (BidAsk diffs) | Excellent (all existing) | Excellent (BidAsk L5) |
| **Orthogonality to existing** | High (withdrawal vs flow) | Medium (enhances OFI) | High (spatial structure) |
| **Implementation effort** | Low-Medium | Low | Low |
| **Standalone alpha potential** | Low (conditional gate) | Medium (if regime is right) | Low-Medium |
| **Feature/overlay potential** | High (FeatureEngine + gate) | High (enhances all OFI signals) | High (new feature family) |
| **Cost sensitivity** | Low (event-driven, rare trades) | Low (regime-gated, fewer trades) | N/A (feature, not strategy) |
| **Latency tolerance** | Medium (1-5s signal) | Good (regime is slow-moving) | Good (state, not event) |
| **Risk of past failure mode** | Low | Medium (OOS regime collapse) | Low |

---

## 5. Recommended Implementation Order

### Phase 1: Candidate C (LOB Active Depth Momentum)
**Rationale**: Lowest risk, highest orthogonality, pure feature addition. Adds a new family of spatial LOB features to FeatureEngine v2 (indices 18-20) without requiring any strategy changes. Provides foundation for both A and B.

### Phase 2: Candidate A (Liquidity Withdrawal Anticipation)
**Rationale**: Provides the conditional gate mechanism needed to make existing signals tradable. Combined with C's spatial features, creates a multi-dimensional regime detector.

### Phase 3: Candidate B (Regime-Conditional OFI)
**Rationale**: Requires the most careful calibration and OOS validation. Benefits from having A and C features available as regime dimensions. This is where the trading strategy comes together.

### Combined thesis
The endgame is an **event-driven, regime-gated taker strategy**:
1. **C** provides spatial LOB state features (is the book structurally biased?)
2. **A** provides temporal event detection (is liquidity about to shift?)
3. **B** provides the conditional trading rule (when A fires AND C confirms AND regime is favorable → trade)

This addresses the fundamental Round 13 finding: standalone signals cannot overcome costs, but *conditional confluence* of orthogonal signals may produce enough edge to trade selectively.

---

## 6. Next Steps

1. **Gate A validation**: Verify that LWI can be reconstructed from consecutive BidAskEvent diffs on TXFD6 data
2. **Feature prototyping**: Implement LOB KE/momentum (Candidate C) as FeatureEngine features
3. **IC measurement**: Compute pooled IC for new features on existing TXFD6 L5 data
4. **Regime frequency analysis**: How often does the "favorable regime" (thin depth + tight spread + OFI alignment) occur on TXFD6?
5. **Cross-reference with DriftBurst**: Do LWI spikes correlate with drift burst events? (Would validate A's mechanism)

---

## Appendix: Papers Reviewed but Not Selected

| Paper | Why not selected |
|-------|-----------------|
| 2601.23172 (Muhle-Karbe, Hawkes flow) | Theoretical — requires Hawkes calibration with 9+ params, deferred same as Round 14 |
| 2603.20456 (Neural HMM) | Deep learning — too complex for our infrastructure, latency-incompatible |
| 2601.02310 (T-KAN) | Deep learning — FI-2010 benchmark, not directly applicable to TXFD6 |
| 2407.16527 (Negative drift of fills) | Confirms our Round 13 findings on adverse selection, but no new alpha direction |
| 2505.05784 (FlowHFT imitation learning) | Requires expert strategies to imitate — we don't have profitable base strategies |
| 2510.26438 (RL market making) | Requires <5ms latency for the Sharpe >30 results; infeasible at 36ms |
| 2502.15757 (TLOB transformer) | DL approach, equities-focused; transaction cost analysis shows limited tradability |

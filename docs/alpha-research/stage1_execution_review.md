# Execution Review: Round 14 Alpha Candidates

**Reviewer**: Execution
**Date**: 2026-03-25
**Latency Profile**: `shioaji_sim_p95_v2026-03-04` (P95 submit RTT = 36ms, modify = 43ms, cancel = 47ms, internal pipeline = 250us)
**TXFD6 Median Tick Interval**: 125ms

---

## Candidate #1: `orderflow_markov_entropy`

**Paper**: Singha 2025, arXiv 2512.15720
**Signal**: 15-state Markov chain entropy over 120-sec trade window. Low entropy = informed trading.

### Latency Assessment: PASS

- Signal half-life is on the order of **seconds to minutes** (120-sec rolling window, regime-scale signal).
- At 36ms P95 RTT, execution latency is <0.03% of the signal horizon. No latency concern.
- Intended use as a **volatility overlay** (not a standalone entry signal) further relaxes timing requirements.

### Feature Engine Fit: PASS (with extension)

- Current FeatureEngine (`lob_shared_v2`, 18 features) consumes `LOBStatsEvent` and `BidAskEvent`.
- Markov entropy is trade-driven (consumes `TickEvent`), which is a **new source_kind** not currently in the engine.
- However, adding a `source_kind="tick"` feature is architecturally clean: `FeatureSpec` already has a `source_kind` field. The engine's `process_lob_event` would need a parallel `process_tick_event` path.
- Can be added as index 18 in a v3 feature set without breaking backward compatibility.
- **Warmup**: ~960 ticks at 125ms median interval = ~120s. Acceptable.

### Data Availability: PASS

- Requires: trade price, trade volume, trade direction (buy/sell classification).
- `TickEvent` provides: `price` (int x10000), `volume`, `bid_side_total_vol`, `ask_side_total_vol`.
- Trade direction can be inferred from cumulative volume deltas (`bid_side_total_vol` / `ask_side_total_vol` changes). This is already how OFI features work.
- **No new data sources required.** Shioaji tick feed is sufficient.

### Config Compatibility: PASS

- As an overlay, it modulates existing strategy signals (e.g., scale position size or widen/narrow spread thresholds).
- No new strategy entry needed in `strategies.yaml` -- integrates into existing strategy params.
- Risk limits unchanged: overlay only affects signal confidence, not position sizing beyond existing limits.
- Config drift = 0.

### Constitution Compliance: PASS

- **Allocator Law**: 15-state transition matrix is 15x15 = 225 ints. Pre-allocatable. Rolling window can use a ring buffer of trade classifications (fixed size ~960 entries for 120s at 8 ticks/sec). No heap allocation in tick loop.
- **Cache Law**: Transition matrix fits in L1 cache (225 * 8 bytes = 1.8KB). Excellent locality.
- **Async Law**: Entropy computation is O(15) log operations per update = sub-microsecond. No blocking.
- **Precision Law**: Entropy output is a float ratio, acceptable per Rule 25 Section 11 (alpha float exception for signal computation, not accounting).
- **Boundary Law**: No Rust boundary crossing needed initially; pure Python is fast enough for 15-state entropy.

### Implementation Estimate

- **LOC**: ~150-200 (feature kernel + state management + FeatureEngine integration)
- **Complexity**: Low-Medium. Well-defined math, no external dependencies.
- **New dependencies**: None.
- **Hot-path impact**: Negligible -- one entropy recomputation per tick (~1us).

### Verdict: APPROVE

Clean fit. Low complexity, no Constitution violations, no latency concerns. The overlay design means zero config drift. Only extension needed is a `process_tick_event` path in FeatureEngine, which is a natural evolution.

---

## Candidate #2: `lob_kinetic_energy`

**Paper**: Li et al. 2023, arXiv 2308.14235
**Signal**: LOB kinetic energy (0.5*q*v^2) and momentum (q*v) with active depth filtering. 1-30 sec predictive horizon.

### Latency Assessment: CONDITIONAL PASS

- Predictive horizon: **1-30 seconds**. At the short end (1s), 36ms RTT consumes 3.6% of the signal window.
- For TXFD6 at 125ms tick interval, a 1-sec signal = ~8 ticks. With 36ms execution, we lose roughly 0.3 ticks of edge.
- **Viable at the 5-30s horizon end.** Sub-5s signals face meaningful decay during execution.
- Round 13 finding: queue priority is THE bottleneck for MM at 36ms RTT. This alpha's short-horizon variant faces the same structural limitation.

### Feature Engine Fit: PASS (with extension)

- Requires multi-level LOB data (5-level depth). `BidAskEvent.bids`/`asks` provides shape (N, 2) arrays with price + volume at each level.
- "Velocity" of queue changes requires tracking previous depth snapshots per level -- similar to existing MLDM feature which already tracks L2-L5 depth deltas.
- Can extend the existing `_LobKernelState` with kinetic energy / momentum accumulators.
- Natural fit as indices 18-19 in a v3 feature set (two features: KE and momentum).

### Data Availability: CONDITIONAL PASS

- Requires: 5-level LOB snapshots with quantities at each price level.
- `BidAskEvent` provides `bids`/`asks` as `np.ndarray` shape (N, 2) -- price and volume per level.
- **Shioaji provides 5-level depth for futures (TXFD6).** BidAskEvent carries L1-L5.
- **Active depth filtering** (distinguishing "active" vs "passive" depth changes) requires tick-by-tick LOB diff. This is computationally heavier than static snapshots but feasible since we already compute OFI deltas.
- **Concern**: "velocity" (rate of change of queue size) requires high-frequency LOB updates. Shioaji quote update frequency for TXFD6 is sufficient (~8/sec), but velocity estimation noise at this granularity may degrade the signal vs. the paper's assumptions of continuous LOB feeds.

### Config Compatibility: PASS

- Can integrate as either a standalone signal or overlay feature.
- If standalone: needs a new strategy entry, but follows existing `strategies.yaml` schema.
- Risk limits: 1-30s horizon aligns with existing intraday PnL limits (soft 500 NTD, hard 1000 NTD).
- Config drift = 0.

### Constitution Compliance: PASS

- **Allocator Law**: State tracking for 5 levels x 2 sides = 10 floats + 2 accumulators. Trivially pre-allocatable.
- **Cache Law**: All state fits in a single cache line (~96 bytes). Excellent.
- **Async Law**: Computation per update is O(5) multiplications + additions. Sub-microsecond.
- **Precision Law**: Signal values are float (acceptable per alpha exception). Queue quantities are int.
- **Boundary Law**: No crossing needed.

### Implementation Estimate

- **LOC**: ~200-250 (feature kernel with velocity tracking, active depth filtering, integration)
- **Complexity**: Medium. Active depth filtering logic needs careful implementation to distinguish real depth changes from price level shifts.
- **New dependencies**: None.
- **Hot-path impact**: Low -- O(5) arithmetic per LOB update.

### Verdict: CONDITIONAL APPROVE

Approved with condition: **target the 5-30s predictive horizon only.** Sub-5s signals will be eaten by execution latency at Shioaji P95 RTT. The active depth filtering adds implementation complexity but no Constitution violations. Data availability is confirmed for TXFD6 5-level depth.

---

## Candidate #3: `hawkes_flow_imbalance`

**Paper**: Muhle-Karbe et al. 2026, arXiv 2601.23172
**Signal**: Core vs reaction flow decomposition via Hawkes process. Surprise-weighted OFI. 30s+ half-life.

### Latency Assessment: PASS

- Signal half-life: **30+ seconds**. 36ms RTT is 0.12% of horizon. No concern.
- Longer-horizon signal is well-suited to our execution latency profile.

### Feature Engine Fit: CONDITIONAL PASS

- Hawkes process estimation requires **iterative maximum-likelihood fitting** or at minimum recursive kernel evaluation.
- The recursive Hawkes intensity update is O(N) per event where N = number of events in the kernel window. For a 30s window at ~8 ticks/sec, N ~ 240 events.
- This is **significantly heavier** than current FeatureEngine features (all O(1) per update).
- Two implementation paths:
  1. **Approximate**: Use exponential kernel Hawkes (recursive O(1) update). Loses multi-timescale decomposition but is FeatureEngine-compatible.
  2. **Full**: O(240) per tick. At ~1us per operation, ~240us per update. This is close to the internal pipeline budget (250us total) and would **double pipeline latency**.
- Recommend: exponential kernel approximation only. Full Hawkes is too heavy for hot path.

### Data Availability: PASS

- Requires: trade timestamps, trade prices, trade volumes, trade direction.
- All available from `TickEvent` (same as candidate #1).
- No new data sources needed.

### Config Compatibility: PASS

- 30s+ signal horizon fits existing strategy framework.
- Can be an overlay (like #1) or standalone entry signal.
- Risk limits compatible with existing config.
- Config drift = 0.

### Constitution Compliance: CONDITIONAL PASS

- **Allocator Law**: Full Hawkes requires maintaining an event history buffer (~240 entries for 30s window). Must be pre-allocated as a ring buffer. If pre-allocated: PASS.
- **Cache Law**: 240-entry ring buffer = ~3.8KB. Fits in L1 cache. PASS.
- **Async Law**: **CONCERN.** Full Hawkes O(240) computation per tick could approach 200-300us. Combined with other pipeline stages, risks breaching the 1ms async budget. Exponential kernel approximation (O(1)) resolves this.
- **Precision Law**: Signal values are float (acceptable per alpha exception).
- **Boundary Law**: If full Hawkes is needed, should be promoted to Rust kernel for performance. Exponential approximation can stay in Python.

### Implementation Estimate

- **LOC**: ~250-350 (Hawkes kernel estimation, core/reaction decomposition, surprise weighting, integration)
- **Complexity**: High. Hawkes process parameter estimation is mathematically complex. Core vs reaction decomposition requires careful statistical methodology. Risk of implementation bugs in the likelihood computation.
- **New dependencies**: None strictly required, but `scipy.optimize` would simplify MLE fitting (cold-path only for parameter calibration).
- **Hot-path impact**: O(1) with exponential kernel approximation; O(240) with full Hawkes (REJECT for hot path).

### Verdict: CONDITIONAL APPROVE

Approved **only with exponential kernel approximation** (O(1) recursive update). Full Hawkes process is too computationally heavy for the hot path and risks Async Law violation. Parameter calibration (MLE fitting) must be done offline/cold-path only, with parameters loaded at startup. This is the highest-complexity candidate of the three.

---

## Summary Matrix

| Criterion | #1 markov_entropy | #2 lob_kinetic_energy | #3 hawkes_flow_imbalance |
|-----------|:-:|:-:|:-:|
| Latency | PASS | CONDITIONAL | PASS |
| Feature Engine Fit | PASS | PASS | CONDITIONAL |
| Data Availability | PASS | CONDITIONAL | PASS |
| Config Compatibility | PASS | PASS | PASS |
| Constitution Compliance | PASS | PASS | CONDITIONAL |
| Implementation LOC | 150-200 | 200-250 | 250-350 |
| **Verdict** | **APPROVE** | **CONDITIONAL** | **CONDITIONAL** |

## Execution Ranking

1. **#1 `orderflow_markov_entropy`** -- Cleanest fit. Lowest risk, lowest complexity, no conditions. Recommended as first implementation.
2. **#2 `lob_kinetic_energy`** -- Good fit at 5-30s horizon. Active depth filtering adds moderate complexity. Condition: avoid sub-5s predictions.
3. **#3 `hawkes_flow_imbalance`** -- Highest theoretical value but highest implementation complexity and Async Law risk. Condition: must use exponential kernel approximation, not full Hawkes.

## Key Architectural Note

Candidates #1 and #3 both require a `process_tick_event` path in FeatureEngine (currently only `process_lob_event` exists). This is a shared prerequisite that should be implemented once for both. Estimated effort: ~50-80 LOC in `engine.py`.

# Round 17 -- Stage 1 Execution Review

**Date**: 2026-03-26
**Reviewer**: Claude (Execution reviewer agent)
**Scope**: Tradability, implementation feasibility, platform compatibility for EGVT, CSLL, FPOPE

---

## Candidate 1: EGVT (Entropy-Gated Volatility Timing)

### Signal Latency Budget

**PASS.** EGVT uses a 120-300s rolling window. The signal changes on the order of seconds, not milliseconds. Shioaji P95 submit RTT of 36ms is negligible relative to the signal timescale. The FeatureEngine processes LOBStatsEvent per tick (~550ms on TMFD6), which is more than sufficient update frequency for a slowly-evolving entropy gate.

No latency budget concern. Config drift: **0**.

### Data Availability

**CONDITIONAL.** The survey claims TickEvent provides price + volume, sufficient for the 15-state Markov matrix (price-sign x volume-quintile). Verified:

- `TickEvent` (`events.py:22`): has `price: int` (scaled x10000) and `volume: int`. **Price sign** can be derived from consecutive tick price deltas. **Volume quintile** requires a rolling calibration window to define quintile boundaries -- this is new computation, not currently in the pipeline.
- **Gap**: The paper uses trade-level data (38.5M individual trades). We have tick snapshots at ~1.8/sec on TMFD6. This gives ~216 transitions in a 120s window. The survey correctly flags this as a risk. For a 15-state transition matrix that is 15x15=225 cells, 216 transitions produce a severely undersampled matrix. Extending to 300-600s (540-1080 transitions) partially mitigates but remains thin.
- **Entropy from LOB snapshots vs trades**: Not a data availability blocker, but a signal quality concern. Execution review defers this to the challenger/researcher for quantitative validation.

### Feature Engine Integration

**PASS with design notes.** EGVT would be a new feature in the FeatureEngine. The architecture supports this cleanly:

1. `FeatureRegistry` (`registry.py`) uses a versioned `FeatureSet` pattern. Adding `entropy_15state_x1000` as feature [21] in a new `lob_shared_v3` is straightforward.
2. `FeatureEngine.process_lob_update()` (`engine.py:330`) is the entry point. EGVT would need access to `TickEvent` data, not just `LOBStatsEvent`. Currently, `process_lob_update` takes `event: object | None` as first arg (the raw BidAskEvent) plus `stats` -- but does NOT receive TickEvents.
3. **Architecture implication**: EGVT needs a tick-level input path into FeatureEngine. Two options:
   - (A) Add a `process_tick()` method to FeatureEngine, maintaining tick-derived state separately. Minimal blast radius.
   - (B) Build EGVT as a standalone module consumed by the strategy, outside FeatureEngine. Simpler but breaks the feature unification vision.
4. **Recommendation**: Option (A). Add `process_tick()` to FeatureEngine. The `_LobKernelState` dataclass pattern (`engine.py:94`) already shows how per-symbol rolling state is maintained. A `_MarkovEntropyState` dataclass with pre-allocated 15x15 transition matrix (numpy int array) fits this pattern.

### CBS Integration

**PASS.** `CascadeBounceStrategy` (`cascade_bounce.py:59`) consumes `LOBStatsEvent` via `on_stats()`. It also receives `FeatureUpdateEvent` via `on_features()` (inherited from `BaseStrategy`, `base.py:182`). To add EGVT gating:

1. Override `on_features()` in CBS to read the entropy value from `FeatureUpdateEvent.get("entropy_15state_x1000")`.
2. Add a threshold check in `_check_entry()`: skip entry if entropy > threshold (high entropy = noise, no big move expected).
3. **Implementation effort**: ~20 lines in CBS. No structural change.

### Hot Path Compliance (Allocator Law)

**CONDITIONAL.** The Markov matrix + entropy computation needs careful implementation:

1. **Transition matrix**: 15x15 = 225 int cells. Pre-allocate as `numpy.zeros((15, 15), dtype=np.int64)` per symbol. On each tick, increment one cell, decrement the oldest (ring buffer of transitions). **No allocation on hot path** -- compliant.
2. **Entropy computation**: Requires row normalization (divide counts by row sums) + Shannon entropy sum. This involves 15 float divisions + 15 log computations per tick. At 1.8 ticks/sec this is ~27 float ops/sec -- trivially fast, well under 1ms.
3. **Volume quintile calibration**: Requires maintaining a sorted window of recent volumes. Use a pre-allocated ring buffer + periodic quantile computation (every N ticks, not every tick). Compliant if implemented correctly.
4. **Risk**: Naive implementation with `collections.Counter` or dict allocations would violate Allocator Law. Must use pre-allocated numpy arrays.

### Verdict: **CONDITIONAL APPROVE**

Conditions:
1. Researcher must validate minimum window size for reliable 15-state entropy on TMFD6 tick rate (quantitative, not hand-wave).
2. Implementation must use pre-allocated numpy arrays for transition matrix and ring buffers.
3. FeatureEngine needs a `process_tick()` entry point (small but non-trivial architecture addition).

**Implementation effort: M (Medium)**
- FeatureEngine `process_tick()` + `_MarkovEntropyState`: ~150 LOC
- Feature registry v3 bump: ~20 LOC
- CBS gating logic: ~20 LOC
- Tests: ~100 LOC
- Total: ~300 LOC, 1-2 sessions

---

## Candidate 2: CSLL (Futures Calendar Spread Lead-Lag)

### Data Pipeline

**REJECT (infrastructure gap).** TX (large TAIEX futures) is NOT in the current data pipeline. Adding it requires:

1. New Shioaji subscription for TX or TXFD6 contract.
2. Normalization pipeline for a second symbol.
3. Cross-symbol event correlation (aligning TX and TMFD6 timestamps).
4. ClickHouse schema update for multi-symbol storage (already supported, but untested for cross-symbol strategies).

This is not a code change -- it is a **data infrastructure project**. The platform supports multi-symbol per-symbol strategies, but has never run a cross-instrument signal.

### Multi-Symbol Architecture

**REJECT (architecture gap).** Current strategy architecture (`BaseStrategy`, `StrategyRunner`) processes events per-symbol. A strategy that consumes TX events to generate TMFD6 orders requires:

1. Strategy must subscribe to TWO symbols but trade only ONE.
2. `StrategyRunner` dispatches events by symbol. A cross-symbol strategy needs a different dispatch model (or must register for both symbols and internally correlate).
3. Position tracking, risk limits, and circuit breakers are per-symbol. Cross-symbol exposure correlation is not implemented.

This is not a fatal architectural flaw, but it is a **non-trivial platform change** that goes beyond alpha research scope.

### Latency Concern

**MARGINAL.** If TX leads TMFD6 by 1 tick (~550ms), and our submit RTT is 36ms, we have ~514ms to detect the signal and submit. This is workable but leaves no margin for:
- Cross-symbol timestamp alignment jitter
- FeatureEngine processing delay for two symbols
- Queue depth during high-activity periods

### Verdict: **REJECT**

Reason: Requires both data infrastructure (TX subscription) and architecture changes (cross-symbol strategy dispatch). Estimated effort is L (Large) -- 3+ sessions minimum, with risk of platform-level regressions. Not appropriate for Round 17 alpha research scope. Defer to Round 18+ as a dedicated infrastructure milestone.

**Implementation effort: L (Large)**
- TX subscription + normalization: ~200 LOC + config
- Cross-symbol strategy dispatch: ~300 LOC + tests
- Risk/position cross-symbol handling: ~200 LOC
- Total: ~700+ LOC, 3+ sessions, high regression risk

---

## Candidate 3: FPOPE (Fill Probability-Optimized Passive Execution)

### OrderAdapter Scope

**CONDITIONAL.** The OrderAdapter (`adapter.py:50`) already supports TIF switching. Verified:

1. `TIF` enum (`contracts/strategy.py:11`): `LIMIT=0, IOC=1, FOK=2, ROD=3` -- limit orders already supported.
2. `OrderAdapter.execute()` (`adapter.py:298`): reads `price_type` from `order_params` and encodes via `_broker_codec.encode_price_type()`. Currently defaults to `"LMT"`. Already supports `"MKT"` / `"MKP"` with IOC/FOK constraint.
3. **Key finding**: The limit/market switching decision happens BEFORE the OrderAdapter -- it must be made by the strategy (or an execution layer between strategy and risk). The OrderAdapter is a pass-through that respects whatever TIF/price_type the OrderIntent specifies.
4. **No OrderAdapter modification needed.** The strategy/execution optimizer sets `price_type` and `tif` on the OrderIntent. The adapter already handles both.

### Fill Probability Computation

**CONDITIONAL.** Data sufficiency:

1. `BidAskEvent` (`events.py:42`): provides L1-L5 bids/asks as numpy arrays `(N, 2)` -- price + volume at each level. **Sufficient** for queue depth modeling.
2. `LOBStatsEvent` provides `imbalance`, `spread_scaled`, `bid_depth`, `ask_depth`. **Sufficient** for state-dependent fill probability features.
3. **Gap**: Queue position estimation. Without explicit exchange queue data (Shioaji does not provide this), fill probability is estimated from observable LOB state only. The paper acknowledges this limitation. The model becomes a statistical approximation, not exact.
4. **Gap**: Historical fill rate calibration. Need to record limit order placements and fills to build the empirical fill probability model. This requires a feedback loop from `FillEvent` back to the execution optimizer. Not currently wired.

### Latency Impact

**PASS.** The fill probability computation is a per-order decision (not per-tick). At CBS's ~15 trades/day, this runs 30 times/day (entry + exit). Even a 1ms computation adds negligible latency. The decision could be a simple lookup table indexed by (spread_state, imbalance_bucket, depth_bucket) -- O(1) with pre-computed table.

### Risk Engine Compatibility

**PASS.** The risk engine validates `OrderCommand` after the strategy emits `OrderIntent`. Switching between limit and market orders changes `price_type` and potentially `tif`, but does not affect:
- Position limits (qty unchanged)
- Exposure limits (notional approximately equal)
- Rate limits (still one order)
- Circuit breaker thresholds

One nuance: market orders have higher execution certainty but worse price. Risk engine should log the order type for audit but does not need structural changes.

### Implementation Architecture

**CONDITIONAL.** FPOPE is best implemented as an **execution optimization layer** between strategy and risk/order pipeline:

1. Strategy emits `OrderIntent` with `price_type="LMT"` (default passive).
2. Execution optimizer intercepts the intent, checks fill probability model.
3. If fill probability < threshold (urgency mode), switches to `price_type="MKT"` + `tif=IOC`.
4. Passes modified intent to risk engine.

This is a new pipeline stage. It sits naturally between `StrategyRunner` output and `RiskEngine` input. The `GatewayService` (if enabled) or the direct `risk_queue` path would need to route through this optimizer.

### Verdict: **CONDITIONAL APPROVE**

Conditions:
1. Must be implemented as a separate execution optimization layer, NOT embedded in OrderAdapter or individual strategies.
2. Requires fill rate feedback loop (FillEvent -> calibration) -- not trivial to wire.
3. Initial version can use a simple heuristic (spread > 2x median -> market order) before building the full state-dependent model.
4. Should be validated in shadow mode first -- incorrect fill probability estimates could increase costs rather than reduce them.

**Implementation effort: M-L (Medium-Large)**
- Execution optimizer layer: ~200 LOC
- Fill probability model (heuristic v1): ~100 LOC
- Fill rate feedback/calibration pipeline: ~200 LOC
- Strategy integration: ~30 LOC
- Tests: ~150 LOC
- Total: ~680 LOC, 2-3 sessions

---

## Platform-Level Assessment

### Smallest Blast Radius

1. **EGVT** -- smallest. Adds a new feature to FeatureEngine (established pattern) + ~20 lines in CBS. No changes to order path, risk engine, or data pipeline.
2. **FPOPE** -- medium. New execution layer between strategy and risk. Touches order flow path but does not modify existing components.
3. **CSLL** -- largest. Requires data infrastructure, cross-symbol dispatch, and risk model changes.

### Infrastructure Reuse

1. **EGVT** -- HIGH. Reuses FeatureEngine registry, CBS strategy, TickEvent pipeline. Only new: Markov state computation.
2. **FPOPE** -- MEDIUM. Reuses OrderAdapter's existing TIF/price_type support. New: execution optimizer layer, fill calibration feedback.
3. **CSLL** -- LOW. Requires new data subscription, new strategy dispatch pattern, new cross-symbol risk handling.

### Implementation Effort Summary

| Candidate | Effort | LOC Estimate | Sessions | Blast Radius |
|-----------|--------|-------------|----------|--------------|
| EGVT      | M      | ~300        | 1-2      | Small        |
| FPOPE     | M-L    | ~680        | 2-3      | Medium       |
| CSLL      | L      | ~700+       | 3+       | Large        |

---

## Overall Verdict

### Stage 1 Survey: **APPROVE with modifications**

1. **EGVT**: CONDITIONAL APPROVE -- proceed to Stage 2 prototype. Conditions: validate minimum entropy window size, use pre-allocated arrays, add `process_tick()` to FeatureEngine.
2. **CSLL**: REJECT -- defer to Round 18+ as infrastructure milestone. Not appropriate for alpha research scope.
3. **FPOPE**: CONDITIONAL APPROVE -- proceed to Stage 2 but as secondary priority behind EGVT. Can start with simple heuristic (spread-based limit/market switching) before full model.

### Recommended Stage 2 Plan

1. **Primary**: EGVT prototype as FeatureEngine feature + CBS gate. Validate entropy signal quality on TMFD6 historical data.
2. **Secondary**: FPOPE heuristic v1 (spread-threshold limit/market switching). Can be developed in parallel since it touches different code paths.
3. **Deferred**: CSLL to Round 18 backlog.

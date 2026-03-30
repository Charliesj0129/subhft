# Execution Review: Stage 2 Prototypes

**Reviewer**: Execution
**Date**: 2026-03-25
**Tests**: 49/49 passed (22 markov_entropy + 27 lob_kinetic_energy)

---

## Candidate #1: `orderflow_markov_entropy`

**File**: `research/alphas/orderflow_markov_entropy/impl.py` (358 LOC)

### Constitution Compliance: PASS

- **Allocator Law**: PASS. `__slots__` on class. Pre-allocated numpy arrays for transition matrix (`np.zeros((_K, _K), dtype=np.int32)`), event buffers (`np.zeros(max_events, ...)`), volume history, and occupancy trackers. No heap allocations in `update()`.
- **Cache Law**: PASS. Transition matrix is contiguous int32 (15x15 = 1.8KB, fits L1). Event buffer arrays are contiguous numpy. Excellent locality.
- **Async Law**: PASS. `_compute_entropy()` is O(K^2) = O(225) simple arithmetic + log operations. Well under 1ms. `_evict_old_events()` is amortized O(1) per tick. No blocking IO.
- **Precision Law**: PASS. Output is float signal score, not a price or accounting value. Permitted per Rule 25 Section 11 (alpha float exception). Input `price` is consumed as `int` (line 215).
- **Boundary Law**: N/A. Pure Python, no Rust crossing.

### Hot-Path Cost Estimate: PASS

- `_volume_quintile()` at line 148: O(N) linear scan over volume history. With default `max_events=4096`, this is O(4096) comparisons per tick. **This is the most expensive operation.** At ~1ns per comparison, ~4us per tick. Acceptable but worth noting for future Rust promotion.
- `_compute_entropy()`: O(225) with log calls. ~2-3us.
- `_evict_old_events()`: Amortized O(1). Worst case during window shift: O(buffer_size) but spread across ticks.
- **Total estimate**: ~5-8us per tick. Well within budget.

### Data Pipeline: PASS

- Uses `price` (int), `volume` (float), `timestamp_ns` (int) via kwargs. These map directly to `TickEvent.price`, `TickEvent.volume`, and `TickEvent.meta.source_ts`.
- Trade direction inferred from price change sign -- no dependency on external classification.
- Volume quintile computation is self-contained (rolling history).

### FeatureEngine Integration: PASS (with notes)

- Requires `process_tick_event` path (does not exist yet in FeatureEngine).
- `source_kind="tick"` would need to be wired. `FeatureSpec` already supports arbitrary `source_kind` strings.
- State management (`_LobKernelState` equivalent) maps cleanly to existing pattern: add `_MarkovEntropyState` per-symbol in FeatureEngine.
- Warmup: 30 events minimum, ~120s at TXFD6 tick rate. Documented correctly.

### Test Quality: PASS

- 22 tests covering: manifest, protocol, basic behavior, informed-trading detection, state-space construction, occupancy stats, orthogonality, reset, window eviction, numerical stability.
- All tests have meaningful assertions (no zero-assert tests).
- Edge cases covered: zero volume, constant price, large values, sparse windows.
- Behavioral test `test_repetitive_pattern_lowers_entropy` validates the core hypothesis.

### Issues Found

1. **MEDIUM -- `_volume_quintile` O(N) scan**: Line 155-160 does a linear scan over the entire volume history buffer. For `max_events=4096`, this is O(4096) per tick. Not a blocker in research, but for FeatureEngine promotion should use a sorted data structure or percentile approximation (e.g., T-Digest or quantile sketch).

2. **LOW -- History lists in `compute_orthogonality`**: The `compute_orthogonality` method allocates `np.array` from a list (line 339). This is cold-path only (analysis method), not called during `update()`. Acceptable.

### Verdict: APPROVE

Clean implementation. All Constitution laws respected. Hot-path cost ~5-8us is well within budget. The O(N) volume quintile scan should be optimized before FeatureEngine promotion but is acceptable for research prototype.

---

## Candidate #2: `lob_kinetic_energy`

**File**: `research/alphas/lob_kinetic_energy/impl.py` (301 LOC)

### Constitution Compliance: CONDITIONAL PASS

- **Allocator Law**: CONDITIONAL. `__slots__` on class. Pre-allocated numpy arrays for prev/cur quantities and velocities. **However**: lines 122-124 use `list[float]` for `_ke_bid_history`, `_ke_ask_history`, `_momentum_history` which grow unboundedly via `.append()` in `update()` (lines 193-195). These are heap allocations on every tick. In research context this is acceptable for analysis, but **must be removed or bounded before FeatureEngine promotion**.
- **Cache Law**: PASS. Quantity arrays are contiguous float64, 5 elements each (40 bytes). All fit in a single cache line.
- **Async Law**: PASS. Core computation is O(5) multiplications + additions per tick. Sub-microsecond.
- **Precision Law**: PASS. Output is float signal score. Input quantities come from `BidAskEvent.bids/asks` which are `np.int64` (converted to float64 via `np.asarray` at line 141). Signal values are not accounting quantities.
- **Boundary Law**: N/A. Pure Python, no Rust crossing.

### Hot-Path Cost Estimate: PASS

- `np.asarray(...).reshape(-1, 2)` at lines 141-142: Array view creation, near-zero cost if input is already numpy.
- Core loop (lines 176-184): 5 iterations of simple arithmetic. ~0.5us.
- `np.copyto` at lines 205-206: 5-element copy, ~0.1us.
- **Total estimate**: ~1-2us per tick. Excellent.

### Data Pipeline: PASS

- Uses `bids` and `asks` kwargs, expects shape (N, 2) with col 0 = price, col 1 = quantity.
- This maps directly to `BidAskEvent.bids` and `BidAskEvent.asks` which are `np.ndarray` shape (N, 2) dtype int64.
- Handles fewer than 5 levels gracefully (lines 143-145).
- **Note**: The `np.asarray(..., dtype=np.float64)` conversion at line 141 creates a copy from int64 to float64. This is a minor allocation (~80 bytes) per tick. Acceptable but could be avoided by working in int64 directly.

### FeatureEngine Integration: PASS

- Consumes `BidAskEvent`, same as existing MLDM feature. Already a supported source in the engine pipeline.
- State structure (`_prev_bid_qty`, `_prev_ask_qty`, velocities, EMAs) maps to `_LobKernelState` extension pattern.
- Can be added as indices 18-19 (KE momentum + KE energy) in a v3 feature set.
- Warmup: 16 ticks. Very fast.

### Test Quality: PASS

- 27 tests covering: manifest, protocol, basic behavior, signal direction (bid growth = positive, ask growth = negative, symmetric = zero), clipping, kinetic energy properties, active depth, MLDM correlation, boundary conditions, reset, numerical stability, EMA convergence.
- All tests have meaningful assertions.
- Physics-consistent tests: static book = zero energy, changing book = nonzero energy, energy tracks activity.
- Good edge cases: zero depth, large values, fewer than 5 levels.

### Issues Found

1. **HIGH -- Unbounded history lists**: Lines 122-124 and 193-195. `_ke_bid_history`, `_ke_ask_history`, `_momentum_history` grow without bound via `.append()` on every `update()` call. Over a trading session (~28,800 ticks at 8/sec for 1 hour), this is ~690KB of growing lists. **Violates Allocator Law in sustained hot-path usage.** Fix: either remove (move to separate analysis wrapper) or cap with a ring buffer.

2. **LOW -- float64 conversion**: Line 141 `np.asarray(kwargs["bids"], dtype=np.float64)` forces a copy when input is int64 (which `BidAskEvent` provides). Could compute directly in int64 for zero-copy, but the 80-byte allocation is negligible.

3. **LOW -- Missing horizon constraint documentation**: The manifest says `feature_set_version="lob_shared_v1"` but doesn't document the 5-30s horizon constraint from Stage 1 review. Should add to hypothesis or a separate `constraints` field.

### Verdict: CONDITIONAL APPROVE

**Condition**: The unbounded history lists (`_ke_bid_history`, `_ke_ask_history`, `_momentum_history`) must be either:
- (a) Removed from the hot-path `update()` method and moved to a separate analysis wrapper, OR
- (b) Bounded with a fixed-size ring buffer (e.g., last 1000 entries)

This is an Allocator Law violation that would cause memory growth during a live trading session. The core signal computation is excellent (O(5), ~1-2us). Once the history issue is fixed, this is a clean APPROVE.

---

## Summary

| Criterion | #1 markov_entropy | #2 lob_kinetic_energy |
|-----------|:-:|:-:|
| Allocator Law | PASS | CONDITIONAL (unbounded lists) |
| Cache Law | PASS | PASS |
| Async Law | PASS | PASS |
| Precision Law | PASS | PASS |
| Hot-path cost | ~5-8us | ~1-2us |
| Data pipeline | PASS | PASS |
| FE integration | PASS (needs tick path) | PASS |
| Tests (pass/total) | 22/22 | 27/27 |
| **Verdict** | **APPROVE** | **CONDITIONAL** |

## Blocking Issues for Stage 3

1. **lob_kinetic_energy**: Must fix unbounded history lists before Gate C backtest (memory growth will corrupt long backtests).
2. **Both**: FeatureEngine `process_tick_event` path needed for markov_entropy integration (shared prerequisite from Stage 1).

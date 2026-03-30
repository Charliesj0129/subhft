# Round 15 — Stage 2 Execution Review: LOB Kinetic Energy Prototype (Tradability)

**Reviewer**: Execution Agent
**Date**: 2026-03-25
**Prototype**: `research/alphas/lob_kinetic_energy/impl.py` (391 LOC)
**Tests**: 36/36 passed
**Validation**: `research/experiments/validations/lob_kinetic_energy/ic_validation_results.json`

---

## 1. Implementation Correctness

### KE Formula: DIVERGENCE FROM SURVEY

The Stage 1 survey (Candidate C) proposed a **spatial** KE measure:
```
KE_bid = sum_i bid_qty[i] * (mid - bid_price[i])^2
```
This weights quantity by squared distance from mid-price — a static snapshot of where depth sits in the book.

The implementation uses a **dynamic/velocity** KE measure:
```
KE_bid = 0.5 * sum_i cur_qty[i] * (cur_qty[i] - prev_qty[i])^2
```
Here, velocity `v_i = delta_q_i` (change in quantity between ticks), and KE = 0.5 * q * v^2. This measures the *energy of quantity changes*, not the spatial distribution of depth.

**Assessment**: The implementation is internally consistent and well-motivated by the physics analogy (mass=quantity, velocity=quantity-change-rate). The docstring and manifest correctly describe this formula. However, it is a **different signal** from what was proposed in the survey. The survey's spatial measure (depth * distance^2) has not been implemented. This is not a bug — it's a design choice — but should be acknowledged. The spatial variant could be added as a separate feature (`lob_gravity_center`) in a later phase.

**Impact on review**: The implementation is correct for what it computes. The IC validation measures the correct (implemented) signal. No correctness issue.

### BBO-Shift Guard: CORRECT

Lines 236-246 implement a per-level price-change guard:
- Compares `cur_bid_price[i]` vs `_prev_bid_price[i]` for each level
- If price at a level index changed, velocity is zeroed (the quantity difference is due to level shifting, not order flow)
- This prevents spurious `v^2` spikes when the BBO moves

**Limitation**: The guard operates on **level indices**, not on price-keyed tracking. If the BBO shifts up by one tick, all level indices shift, and ALL velocities are zeroed for that tick. This is conservative (avoids false signals) but loses information when only L1 shifts while deeper levels remain stable at their price points. A price-keyed approach would preserve velocity at stable price levels, but is more complex.

**Test coverage**: `test_bbo_shift_zeroes_velocity` and `test_bbo_stable_allows_velocity` verify the guard. Both pass.

### Integer Handling: PASS

Input from BidAskEvent arrives as int64 (prices x10000, quantities as integers). The prototype converts to float64 at line 197-198:
```python
bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
```
All subsequent arithmetic is float64. The prices stored in `cur_bid_price`/`cur_ask_price` are used only for equality comparison in the BBO-shift guard — precision is exact for integer values stored as float64 (float64 can represent all integers up to 2^53 exactly).

---

## 2. FeatureEngine Integration Path

### Current State
- FeatureEngine `_compute_values()` only extracts L1 via `_extract_l1_qty()` (engine.py:550-581)
- KE momentum requires full L2-L5 arrays from `BidAskEvent.bids`/`BidAskEvent.asks`
- The `event` object (BidAskEvent) is already passed to `process_lob_update()` — no pipeline change needed to access it

### Required Changes

**Phase 0 — L2-L5 extraction (shared prerequisite)**:
- Add method to extract full depth arrays from BidAskEvent (~30 lines)
- Store previous snapshot per symbol for velocity computation (~80 bytes/symbol)

**Phase 1 — KE features**:
- Add `_LobKEState` per symbol: `prev_bid_qty[5]`, `prev_ask_qty[5]`, `prev_bid_price[5]`, `prev_ask_price[5]`, `momentum_ema: float`, `energy_ema: float`. Total: ~100 bytes/symbol.
- 2 new feature indices (minimum):
  - `ke_momentum_signal` (float, [-2, 2]) — directional momentum
  - `ke_energy_ema` (float) — total energy EMA, volatility proxy
- Optional 2 more indices for `skip_l1` variants (deep-only momentum, deep-only energy)

**Feature set version**: Requires new feature set registration. Current `lob_shared_v1` has 16 features. New set would be `lob_shared_v2` or `lob_shared_v3` depending on whether ISS/MLDM land first.

### Compute Cost

| Component | Per-tick cost |
|-----------|--------------|
| L2-L5 array extraction | ~0.3us |
| BBO-shift guard (5 levels) | ~0.2us |
| KE/momentum loop (5 levels) | ~0.3us |
| EMA + normalization | ~0.1us |
| State copy (prev arrays) | ~0.2us |
| **Total KE addition** | **~1.1us** |
| Current engine total | ~2-5us |
| **New engine total** | **~3-6us** |

At 125ms inter-tick (TXFD6), engine compute uses 0.005% of available time. No hot-path concern.

### Bitmask Capacity
Current features: 16. With KE: 18-20. `changed_mask` and `warmup_ready_mask` are Python `int` (arbitrary precision). No overflow at any feature count.

---

## 3. Overflow Verification

### Test Methodology Review

The validation script (`validate_ic.py` lines 205-266) runs 5 overflow scenarios:

| Test | Input Range | What It Tests |
|------|-------------|---------------|
| `max_realistic` | price=230M, qty=10K-15K | TXFD6 extremes with scaled int prices |
| `extreme_qty_1e15` | qty=1e15 | Far beyond any real instrument |
| `zero_qty` | qty=0 | Division by zero safety |
| `alternating_extreme` | qty alternates 1e12/1.0 | Maximum velocity scenario |
| `large_velocity_x_qty` | qty jumps 1.0 -> 1e12 | KE = 0.5 * 1e12 * (1e12)^2 = 5e35 |

**Assessment**: The test cases are representative and include scenarios far beyond TXFD6 reality:
- Max TXFD6 price: ~23,000 points * 10,000 scale = 230,000,000. Test covers this.
- Max TXFD6 single-level qty: typically < 5,000 contracts. Test uses 10,000-15,000.
- The `large_velocity_x_qty` test produces KE = 5e35, which is extreme but within float64 range (max ~1.8e308).

**Realistic worst case**: qty=10,000, velocity=5,000 (half the queue disappears), KE = 0.5 * 10,000 * 5,000^2 * 5 levels = 6.25e11. Sum across bid+ask: ~1.25e12. This is trivially within float64 range.

The epsilon guard (`_EPSILON = 1e-12`) protects against division by zero in the normalization step. Signal clipping to [-2, 2] provides a final safety net.

**Verdict**: Overflow testing is thorough and representative. All tests pass. No concern.

---

## 4. Signal Half-Life vs Latency

At h=10 ticks (~1.25s at 125ms median tick interval), the signal shows pooled IC = +0.0068. This means the signal has predictive power at a horizon of ~1.25 seconds.

**Latency budget**:
- Signal computation: ~1us (negligible)
- Internal pipeline (feature -> strategy -> risk -> order): ~250us (from latency profile)
- Order submit P95: 36ms
- **Total signal-to-market**: ~37ms

**Available reaction window**: 1,250ms - 37ms = 1,213ms. Roughly 10 ticks of lead time after accounting for our latency.

**Assessment**: Feasible. The signal at h=10 gives ~1.2 seconds of lead time, which is 33x our submit latency. Even at h=50 (~6.25s), IC is still positive (0.0041). The signal is slow-moving enough for our infrastructure.

**Caveat**: IC was measured on L1 only. With real L5 data, the signal dynamics could be different — potentially faster-decaying (L5 changes are more informative but more transient) or slower (deeper levels change more gradually). This must be validated with real L5 data.

---

## 5. Config Parameters

### Research Parameters (from `impl.py`)

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `active_depth` | 5 | Number of book levels to use (1-5) |
| `skip_l1` | False | Exclude L1 from KE computation |
| `ring_size` | 1024 | History ring buffer size |
| `_EMA_FAST` | 1 - exp(-1/4) | ~4-tick EMA for momentum smoothing |
| `_EMA_SLOW` | 1 - exp(-1/16) | ~16-tick EMA for energy smoothing |
| `_SIGNAL_CLIP` | 2.0 | Output clipping bounds |
| `_WARMUP_TICKS` | 16 | Ticks before signal is emitted |

### Production Config Mapping

For FeatureEngine integration, these would map to:

| Param | Where | Notes |
|-------|-------|-------|
| `active_depth` | FeatureProfile params | Configurable per-symbol |
| `skip_l1` | FeatureProfile params | Toggle for OFI-decoupled variant |
| `ema_window` | FeatureProfile params | Already supported pattern (line 217-219 of engine.py) |
| `ring_size` | Not needed in FE | Ring buffer is for research analysis only |
| `signal_clip` | Hardcoded constant | No reason to make configurable |
| `warmup_ticks` | `FeatureSpec.warmup_min_events` | Already supported |

**Config drift = 0**: No new environment variables needed. No new config files. All parameters fit within existing `FeatureProfile` and `FeatureSpec` patterns. The `ring_size` parameter is research-only and would not carry into production.

---

## Summary

| Check | Status | Notes |
|-------|--------|-------|
| KE formula correctness | PASS (with divergence note) | Uses velocity-based KE, not spatial KE from survey |
| BBO-shift guard | PASS | Conservative (level-index based), tested |
| Integer handling | PASS | Float64 throughout, exact for int64 inputs |
| FeatureEngine integration | Clear path | ~1.1us added cost, 2-4 new features |
| Overflow testing | PASS | 5/5 scenarios, representative of TXFD6 extremes |
| Signal half-life vs latency | PASS | h=10 = ~1.25s, 33x our submit latency |
| Config compatibility | PASS | Config drift = 0, fits existing FeatureProfile pattern |
| Prior blocking issue | RESOLVED | Ring buffers replace unbounded lists |

### Design Divergence Note

The implementation computes **dynamic KE** (quantity-change velocity), not the **spatial KE** (depth * distance-from-mid^2) proposed in the Stage 1 survey. Both are valid physics-inspired measures. The spatial variant (`lob_gravity_center`) was listed as a proposed feature in the survey and has not been implemented. This should be tracked as a follow-up if the dynamic KE shows insufficient IC on real L5 data.

### Verdict: APPROVE

The prototype is numerically robust, constitutionally compliant, and integration-ready. The ring buffer fix resolves the only prior blocking issue. Compute cost is minimal (~1.1us/tick). All data is available via existing BidAskEvent. Config drift is zero.

**Next steps for Stage 3**:
1. Obtain real L5 BidAskEvent data for IC re-validation (current L1-only IC is inconclusive)
2. Fix LOW issues before FeatureEngine promotion: pre-allocate `cur_bid_price`/`cur_ask_price` arrays in `__init__` (line 192-193)
3. Consider implementing the spatial KE variant (`lob_gravity_center`) as a complementary feature

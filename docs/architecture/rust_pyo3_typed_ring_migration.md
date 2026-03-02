# PyO3 Typed Ring / Zero-Copy Migration Plan (EventBus)

Date: 2026-02-23
Owner: Codex (implementation planning artifact)
Status: Draft (Phase plan + interfaces)

## 1. Problem Statement

Current Rust `FastRingBuffer` (`rust_core/src/bus.rs`) stores `PyObject` slots and returns cloned Python refs.

Implications:
- Not zero-copy for hot-path event payloads
- Still GIL/refcount heavy
- Python object allocation remains dominant at higher event rates

This violates the intended HFT boundary target for the event bus hot path (typed, cache-friendly, low-allocation).

## 2. Design Review Artifact (HFT Architect 5-Step)

### 1) Allocation Audit
- Current state allocates Python event objects before entering the ring.
- Target: typed ring stores fixed-size event structs (`#[repr(C)]`) or shared-memory frame headers + payload slices.

### 2) Latency Budget
- Goal: reduce publish/consume bus overhead by eliminating Python object ref churn.
- Success criterion: measurable reduction in `StrategyRunner` pipeline overhead under benchmark (`tests/benchmark/latency_test.py` + microbench).

### 3) Threading Model
- Single-writer fast path remains supported.
- Multi-consumer semantics must preserve non-blocking reads and overflow handling.
- No blocking I/O in publish/consume path.

### 4) Data Layout
- Use cache-friendly arrays/rings of fixed event structs.
- Avoid pointer chasing (`Vec<Option<PyObject>>` -> typed contiguous ring storage).

### 5) Failure Mode Analysis
- Overflow: preserve existing HALT integration and overflow counters.
- Version mismatch: feature-flag fallback to Python ring/list path.
- ABI mismatch: runtime feature probe + explicit fallback logging.

## 3. Target Architecture (Incremental)

### Phase A (Low Risk): Typed Tuple Ring on Python Side
- Keep Python `RingBufferBus` API.
- Introduce event tuple normalization contract (already largely used).
- Store tuples in Python list ring only for parity baselines.

### Phase B (Rust Typed Ring in Extension)
- New Rust ring class (example): `FastTypedRingBuffer`
- Stores fixed struct per slot, e.g. `MdEventFrame`
- Python side passes primitive fields (or memoryview/ndarray handles for side arrays)
- Consume returns lightweight tuple or buffer view depending mode

### Phase C (Shared Memory / Zero-Copy Payload)
- For large payloads (LOB sides), store frame metadata in ring and side data in shared memory region
- Python/Rust exchange via buffer protocol / `memoryview`
- Recorder/strategy can choose tuple/event/buffer decode modes

## 4. Proposed Event Frame (v1)

```rust
#[repr(C)]
pub struct MdEventFrame {
    pub kind: u8,          // tick=1, bidask=2, snapshot=3, stats=4
    pub flags: u8,         // bit flags (snapshot, synthesized, oddlot, simtrade...)
    pub reserved: u16,
    pub symbol_id: u32,    // interned symbol table index
    pub seq: u64,
    pub exch_ts_ns: u64,
    pub local_ts_ns: u64,
    pub price0: i64,       // tick px or best bid
    pub price1: i64,       // best ask / aux
    pub qty0: i64,         // volume / bid depth
    pub qty1: i64,         // ask depth / aux
    pub aux0: i64,         // mid_price_x2 / total_volume / ptr offset
    pub aux1: i64,         // spread_scaled / ptr len
    pub ratio0: f64,       // imbalance or reserved
}
```

Notes:
- Prices and sizes remain scaled ints (Precision Law).
- LOB full depth arrays are out-of-line in later phases (shared memory).
- `symbol_id` requires symbol table handshake (Python<->Rust).

## 5. Python API Compatibility Strategy

Keep `RingBufferBus` stable and add modes:
- `HFT_BUS_MODE=python` (current list ring)
- `HFT_BUS_MODE=rust_pyobj` (current `FastRingBuffer`)
- `HFT_BUS_MODE=rust_typed` (new typed ring)

Consumer decode modes:
- `event` -> dataclass objects
- `tuple` -> low-allocation tuples
- `buffer` -> typed frame view (advanced / experimental)

## 6. Implementation Tasks (Sequenced)

1. Add parity tests for publish/consume semantics independent of payload type
2. Add typed frame schema + symbol interning map
3. Implement `FastTypedRingBuffer` in Rust extension
4. Add Python wrapper adapter in `src/hft_platform/engine/event_bus.py`
5. Add benchmark comparing `python`, `rust_pyobj`, `rust_typed`
6. Add fallback logging + feature flags in runbook/docs

## 7. Validation Plan

### Functional Parity
- Order preservation
- Overflow behavior / HALT trigger parity
- Consumer skip semantics parity (`lag > size`)
- Batch consume parity

### Performance
- Microbench: publish+consume primitives only
- Pipeline bench: `tests/benchmark/latency_test.py`
- Stress: `tests/stress/test_event_bus_stress.py`

### Safety / Recovery
- Extension import failure -> fallback path
- Invalid symbol_id -> drop + metric + error log
- Shared memory attach failure (future phase) -> fallback to tuple path

## 8. Dependencies / Risks

- Requires stable symbol interning strategy across producer/consumer
- Shared memory phase introduces lifecycle/cleanup complexity
- Mixed payload modes (tick small, bidask larger) need explicit framing and decoding rules

## 9. Definition of Done (for Phase B)

- `rust_typed` feature flag available and documented
- Parity tests pass vs existing ring implementations
- `latency_test.py` and microbench show measurable improvement in bus overhead
- Fallback path remains default-safe when extension is unavailable

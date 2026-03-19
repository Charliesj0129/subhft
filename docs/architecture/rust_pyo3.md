# Rust-Python Boundary Map (PyO3, As-Built)

Date: 2026-03-19
Scope: Actual boundary between Python runtime and Rust extensions in this repository.

## 1. Extension Modules

### 1.1 `rust_core` (from `rust_core/src/lib.rs`) — 36 pyclass + 22 pyfunction

#### Classes by Domain

**Event Routing (6)**:
- `FastRingBuffer` — Lock-free SPSC ring buffer
- `EventBus` — Event dispatching hub
- `FastTickRingBuffer` — Typed ring buffer for TickEvent
- `FastBidAskRingBuffer` — Typed ring buffer for BidAskEvent
- `FastLOBStatsRingBuffer` — Typed ring buffer for LOBStatsEvent
- `FastTypedRingBuffer` — Generic typed ring buffer

**LOB (3)**:
- `LimitOrderBook` — Full limit order book state
- `PriceLevel` — Single price level (price + qty)
- `RustBookState` — Compact book state snapshot

**Fused Normalizers (2)**:
- `RustNormalizerLobFused` — Fused normalizer + LOB pipeline
- `RustNormalizerFeatureFusedV1` — Fused normalizer + LOB + feature pipeline

**Feature Engine (3)**:
- `RustFeatureEngineV2` — 16-feature vector computation engine
- `LobFeatureKernelV1` — LOB feature kernel (stateless features)
- `RustFeaturePipelineV1` — End-to-end feature pipeline

**Risk / Safety (4)**:
- `FastGate` — Fast risk gate check
- `RustRiskValidator` — Risk validation engine
- `RustStormGuardValidator` — StormGuard FSM validator
- `RustCircuitBreaker` — Circuit breaker FSM

**Gateway (3)**:
- `RustExposureStore` — Exposure tracking with cardinality bound
- `RustGatewayFusedCheck` — Fused gateway check (dedup + exposure + risk)
- `RustDedupStore` — Order deduplication store

**Alpha (8)**:
- `AlphaDepthSlope` — Depth slope signal
- `AlphaOFI` — Order flow imbalance
- `AlphaRegimePressure` — Regime pressure detector
- `AlphaRegimeReversal` — Regime reversal detector
- `AlphaTransientReprice` — Transient reprice signal
- `AlphaMarkovTransition` — Markov LOB state transition
- `MatchedFilterTradeFlow` — Matched filter trade flow
- `MetaAlpha` — Meta-alpha combiner

**Strategy (1)**:
- `AlphaStrategy` — Rust-native strategy executor

**IPC / Shared Memory (5)**:
- `ShmRingBuffer` — Shared memory ring buffer for IPC
- `ShmSnapshotTable` — Shared memory snapshot table
- `SymbolInternTable` — Symbol string interning
- `RustColumnarBuffer` — Columnar storage buffer
- `RustMetricsSampler` — Metrics sampling

**Position (1)**:
- `RustPositionTracker` — O(1) position accounting

#### Functions (22)

**LOB Scaling / Stats**:
- `scale_book`, `scale_book_seq`, `scale_book_pair`, `scale_book_pair_stats`, `scale_book_pair_stats_np`
- `compute_book_stats`, `get_field`

**Normalization**:
- `normalize_tick_tuple`, `normalize_bidask_tuple`, `normalize_bidask_tuple_np`, `normalize_bidask_tuple_with_synth`

**Record Mapping / Backtest / Time Utilities**:
- Additional functions from `record_mapper.rs`, `backtest_kernels.rs`, `timeutil.rs`

### 1.2 `rust_strategy` (from `rust/src/lib.rs`)
- Classes: `RLStrategy`, `RLParams`
- Note: no active import under `src/hft_platform/*` in current codebase

## 2. Python Call Sites (Current)

1. **Market data normalization path**
   - `src/hft_platform/feed_adapter/normalizer.py`
   - Uses Rust helpers for scaling and normalization when available.

2. **LOB statistics path**
   - `src/hft_platform/feed_adapter/lob_engine.py`
   - Uses `compute_book_stats` acceleration.

3. **Event bus implementation**
   - `src/hft_platform/engine/event_bus.py`
   - Uses `FastRingBuffer` when `HFT_RUST_ACCEL` and `HFT_BUS_RUST` are enabled.

4. **Position tracking path**
   - `src/hft_platform/execution/positions.py`
   - Uses `RustPositionTracker` when import succeeds.

5. **Strategy acceleration path**
   - `src/hft_platform/strategies/rust_alpha.py`
   - Uses `AlphaStrategy` in Rust extension.

6. **Feature engine path**
   - `src/hft_platform/feature/engine.py`
   - Uses `RustFeatureEngineV2` / `LobFeatureKernelV1` when `HFT_FEATURE_ENGINE_BACKEND=rust`.

7. **Risk validation path**
   - `src/hft_platform/risk/engine.py`
   - Uses `RustRiskValidator`, `RustStormGuardValidator` when available.

8. **Gateway path**
   - `src/hft_platform/gateway/service.py`
   - Uses `RustGatewayFusedCheck`, `RustExposureStore`, `RustDedupStore`.

9. **Fused normalizer path**
   - `src/hft_platform/services/market_data.py`
   - Uses `RustNormalizerLobFused` / `RustNormalizerFeatureFusedV1` for zero-copy pipeline.

10. **Monitor / IPC path**
    - `src/hft_platform/monitor/_data_source.py`
    - Uses `ShmSnapshotTable` for shared memory data reads.

## 3. Loading and Fallback Behavior

1. Preferred import path
- `hft_platform.rust_core`

2. Fallback import path
- `rust_core`

3. Failure behavior
- Python modules keep pure-Python fallback paths when Rust module import fails.
- Some strategy paths (`rust_alpha`) explicitly require extension and raise if unavailable.

## 4. Build and Verification

1. Build `rust_core`
```bash
uv run maturin develop --manifest-path rust_core/Cargo.toml
```

2. Build `rust_strategy`
```bash
uv run maturin develop --manifest-path rust/Cargo.toml
```

3. Quick verification
```bash
python -c "import hft_platform.rust_core as rc; print(hasattr(rc, 'FastRingBuffer'))"
python -c "import rust_strategy as rs; print(hasattr(rs, 'RLStrategy'))"
```

## 5. Boundary Rules for This Repo

1. Keep hot-path payload transforms in Rust when measurable and deterministic.
2. Keep Python fallback behavior explicit and observable.
3. Avoid adding copy-heavy Python<->Rust conversions in event-loop paths.
4. Preserve scaled-int semantics across boundary for monetary-critical logic.

## 6. Known Gaps

1. No single benchmark gate currently asserts Rust path parity for all fallback paths.
2. Some modules rely on dynamic import flags and env toggles; operational defaults should stay documented in runbooks.
3. FeatureEngine Rust kernels (`RustFeatureEngineV2`, `LobFeatureKernelV1`) are production-ready; packed/zero-copy transport (`TypedFeatureFrameV1`) is implemented but finalization remains open.
4. Python/Rust shadow parity validation is available via `HFT_FEATURE_SHADOW_PARITY=1`; production-grade parity hardening for all 16 features is tracked in `docs/TODO.md`.

## 7. Boundary Extension: Feature Plane / FeatureEngine (Prototype Landed)

Reference spec: `docs/architecture/feature-engine-lob-research-unification-spec.md`

Planned boundary goals:
1. Add a stable feature ABI (`FeatureUpdateFrameV1`) between Python runtime and future Rust feature kernels.
2. Preserve scaled-int semantics for price-derived feature outputs where exactness matters.
3. Support research replay / `hftbacktest` / live parity with the same feature-set versioning.
4. Avoid copy-heavy feature transport in hot paths (typed frames / packed buffers preferred).

Planned scope (initial):
1. Shared microstructure features after `LOBEngine` (spread/microprice/imbalance/OFI-like bounded-state kernels)
2. Python FeatureEngine wrapper first, Rust kernels promoted after parity validation
3. Feature registry + feature-set versioning for strategy/runtime compatibility

Non-goals (initial):
1. Moving strategy decision logic into Rust feature kernels
2. Replacing all strategy SDK event contracts in one step

---
name: hft-rust-exports
description: Reference table of Rust (`rust_core` via PyO3) exports — ring buffers, LOB scaling, normalizers, risk validators, alpha kernels, feature engines, ClickHouse mappers, shared memory primitives. Consult when invoking or reviewing Rust-backed Python APIs in the HFT platform.
---

# Rust Boundary (`rust_core` via PyO3)

Compiled extension at `src/hft_platform/rust_core.cpython-*.so`.

| Export                                                                                                                                                               | Purpose                                        |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `FastRingBuffer`, `EventBus`, `FastTickRingBuffer`, `FastBidAskRingBuffer`, `FastLOBStatsRingBuffer`                                                                 | Lock-free event routing / typed ring buffers   |
| `scale_book`, `scale_book_seq`, `scale_book_pair`, `scale_book_pair_stats`, `scale_book_pair_stats_np`, `compute_book_stats`, `get_field`                            | LOB scaling and book stats hot path            |
| `normalize_tick_tuple`, `normalize_bidask_tuple`, `normalize_bidask_tuple_np`, `normalize_bidask_tuple_with_synth`, `normalize_tick_v2`, `normalize_bidask_v2`       | Tick/BidAsk normalization (Python + v2 paths)  |
| `LimitOrderBook`                                                                                                                                                     | Full limit order book state                    |
| `RustBookState`                                                                                                                                                      | Lightweight LOB snapshot state                 |
| `RustPositionTracker`                                                                                                                                                | O(1) position accounting                       |
| `FastGate`, `RustRiskValidator`, `RustExposureStore`, `RustCircuitBreaker`, `RustStormGuardValidator`                                                                | Risk gate, validator, exposure tracking, breaker, storm guard |
| `RustDedupStore`                                                                                                                                                     | Idempotency / order deduplication              |
| `LobFeatureKernelV1`, `RustFeaturePipelineV1`, `RustFeatureEngineV2`                                                                                                | LOB feature kernels and feature engine         |
| `AlphaDepthSlope`, `AlphaOFI`, `AlphaRegimePressure`, `AlphaRegimeReversal`, `AlphaTransientReprice`, `AlphaMarkovTransition`, `MatchedFilterTradeFlow`, `MetaAlpha` | Alpha signal generators                        |
| `AlphaStrategy`                                                                                                                                                      | Rust-native strategy executor                  |
| `RustColumnarBuffer`                                                                                                                                                 | Columnar data buffer for batch recording       |
| `RustMetricsSampler`                                                                                                                                                 | Low-overhead Prometheus metrics sampler        |
| `to_ch_price_scaled`, `map_tick_record`, `map_bidask_record`, `map_order_record`, `map_fill_record`                                                                  | ClickHouse record mapping                      |
| `coerce_ns_int`, `coerce_ns_float`                                                                                                                                   | Timestamp coercion utilities                   |
| `ShmRingBuffer`, `ShmSnapshotTable`                                                                                                                                  | Shared memory IPC and snapshot table           |
| `SymbolInternTable` *(Wave 4)*                                                                                                                                       | Symbol string interning (O(1) lookup)          |
| `FastTypedRingBuffer` *(Wave 4)*                                                                                                                                     | Typed, cache-friendly ring buffer              |
| `RustGatewayFusedCheck` *(Wave 4)*                                                                                                                                   | Fused gateway risk check (zero-copy)           |
| `RustNormalizerLobFused` *(Wave 4)*                                                                                                                                  | Fused normalizer + LOB pipeline                |
| `RustNormalizerFeatureFusedV1` *(Wave 4)*                                                                                                                            | Fused normalizer + LOB + feature pipeline      |

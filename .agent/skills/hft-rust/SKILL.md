---
name: hft-rust
description: "Rust/PyO3 work in rust_core: when to port, zero-copy FFI, parity tests, benchmark gate, build, exported kernels. Use when porting a hot computation, adding or reviewing a PyO3 export, or debugging a parity or fallback issue. Not for generic Rust style."
---

# Rust / PyO3

Crate rules (no panics reachable from Python, `PyResult`, GIL release, views not
copies) are in `rust_core/AGENTS.md`. The Rust extension is built from
`rust_core/` with `make build-rust` (`uv run maturin develop --manifest-path rust_core/Cargo.toml`).

## When to port

Prove it is slow and prove it is correct before writing Rust:

1. Pure Python/numpy prototype with a deterministic test.
2. Profile (`py-spy record`, `cProfile`); the function must dominate (about 10% of CPU
   or a stage budget miss). If not, stop.
3. Port: accept `PyReadonlyArray1<f64>`/`PyReadonlyArrayDyn` views, return arrays or
   plain types, no allocation in the kernel, logic in pure-Rust modules,
   registration only in `lib.rs`.
4. Bind with `#[pyclass]`/`#[pymethods]`; update stubs/hints if the repo has them.

## Parity and benchmark (the gate)

```python
np.testing.assert_allclose(py_impl(data), rs_impl(data), rtol=1e-10)
```

Also test both enabled and disabled paths and the fail-closed fallback
(`HFT_RUST_ACCEL=0`, `HFT_RUST_FORCE=1`). A feature kernel's parity gate:
`make feature-parity`. Benchmark with `tests/benchmark/` (`make benchmark`,
`make benchmark-compare`); a port should be worth its complexity (the historical
bar was about 10x). Rust fallbacks must be explicit and observable (metric or log).

## Exports

Registered in `rust_core/src/lib.rs`. Families: typed ring buffers and `EventBus`;
book scaling and stats (`scale_book*`, `compute_book_stats`); normalizers
(`normalize_*_tuple`, `*_v2`, fused `RustNormalizerLobFused`,
`RustNormalizerFeatureFusedV1`); `LimitOrderBook`, `RustBookState`;
`RustPositionTracker`; risk (`FastGate`, `RustRiskValidator`, `RustExposureStore`,
`RustCircuitBreaker`, `RustStormGuardValidator`, `RustGatewayFusedCheck`);
`RustDedupStore`; features (`LobFeatureKernelV1`, `RustFeaturePipelineV1`,
`RustFeatureEngineV2`); alpha kernels (`Alpha*`, `MatchedFilterTradeFlow`,
`MetaAlpha`, `AlphaStrategy`); recording (`RustColumnarBuffer`, `map_*_record`,
`to_ch_price_scaled`); `RustMetricsSampler`; shared memory (`ShmRingBuffer`,
`ShmSnapshotTable`); `SymbolInternTable`; `coerce_ns_*`. Read `lib.rs` for the
current list.

## Deploy note

The built `.so` is bind-mounted on the production host (deploy Class A): a Rust
change ships as a rebuilt artifact. Follow `.agent/rules/41-deployment.md`.

## Done when

`make build-rust`, `cargo clippy`, `cargo test`, the Python/Rust parity test, and
the benchmark comparison all pass, and the fallback path is tested.

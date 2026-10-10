---
name: hft-latency-profiling
description: "Profile hot-path latency (stage budgets, Prometheus metrics, py-spy, flamegraph) and apply the broker-RTT latency realism guard. Use when investigating a latency regression, judging whether a hot computation needs optimizing, or validating latency before production. Not for correctness review."
---

# Latency profiling

## Budget and measurement

Internal target is on the order of 200 us from callback to order submission;
broker RTT (tens of milliseconds) dominates end-to-end time. Per-stage
Prometheus histograms exist for normalize, LOB, feature engine, strategy, and
risk (`src/hft_platform/observability/metrics.py` has the exact names); targets
differ for the Rust and Python paths. Do not trust a copy of a budget table;
read the current metric and the baseline in
`docs/architecture/latency-baseline-shioaji-sim-vs-system.md`.

```bash
curl -s localhost:9090/metrics | grep -E "latency"           # current stage values
make hotpath-profile        # per-stage profile
make benchmark              # regression benchmarks; make benchmark-compare against baseline
```

Check which path is active before comparing (fused normalizer, feature backend
flags in `hft-market-data`); `event_loop_lag_ms` is not literally loop lag.

## Tools

- Python: `py-spy record -o flame.svg --pid <PID>` (non-intrusive), `py-spy top`,
  `python -m cProfile -o profile.out ...`.
- Rust: `cargo flamegraph --bench <bench>`, `cargo bench` in `rust_core/`.
- Allocation: `python -X tracemalloc=10 ...`; GC activity via `gc.get_stats()`.

## Anti-patterns that cost latency

`datetime.now()` (use `now_ns()`), `Decimal` or `pandas` in the loop, `print()`,
try/except for control flow in a tight loop, list comprehensions per tick,
arrays of objects, `json.loads` on large payloads (use orjson off-loop),
blocking `requests`/`time.sleep` on the loop. Full rules: `AGENTS.md`,
`hft-hot-path-dev`.

## Latency realism guard

Internal microseconds do not imply executable latency. In research and backtests:
model place, update, and cancel latencies separately; use at least P95 for
promotion decisions and P99 for stress; record the assumption in the artifact;
no latency profile means not promotion-ready; treat sub-RTT alpha half-lives as
optimistic until shadow or live evidence confirms them (`hft-backtest`).

## Optimization order

Preallocated buffers, then numba/vectorized Python, then GC control during
trading, then CPU isolation. The existing fused Rust path is kept as is; new Rust
ports are paused. Each step needs a before/after
measurement under the same load.

## Done when

The change is justified by a profile naming the dominant function, the
before/after numbers come from the same method, and `make benchmark-compare`
shows no stage regressed.

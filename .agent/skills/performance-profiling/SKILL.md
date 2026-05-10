---
name: hft-latency-profiling
description: Profile and optimize HFT platform hot path latency. Covers latency budgets, Prometheus metrics analysis, anti-patterns, profiling tools, and the latency realism guard for broker RTT.
---

# HFT Latency Profiling

## When to Use

- Investigating performance regressions
- Evaluating Rust migration candidates (Python -> PyO3)
- Pre-production latency validation
- Optimizing hot path stages
- Comparing Python vs Rust code paths

## Hot Path Latency Budget

Total internal target: < 200us end-to-end (exchange callback to order submission).

| Stage | Target (Rust) | Target (Python) | Prometheus Metric |
|-------|---------------|-----------------|-------------------|
| Normalize | < 5us | < 50us | `normalize_latency_ns` |
| LOB Process | < 10us | < 100us | `lob_process_latency_ns` |
| Feature Engine | < 20us | < 200us | `feature_engine_latency_ns` |
| Strategy | < 100us | < 100us | `strategy_latency_ns` |
| Risk Check | < 50us | < 50us | `risk_check_latency_ns` |
| **Total Internal** | **< 200us** | **< 500us** | -- |
| Broker RTT (Shioaji sim P95) | ~36ms | ~36ms | `broker_order_rtt_ns` |

The broker RTT is roughly 500x larger than total internal latency. This is the dominant factor in end-to-end order execution time.

## Measuring Latency

### Prometheus Queries

```bash
# Current latency values
curl -s http://localhost:9090/metrics | grep -E "normalize_latency|lob_process_latency|strategy_latency|risk_check_latency"

# Feed-to-strategy pipeline total
curl -s http://localhost:9090/metrics | grep pipeline_total_latency
```

### Per-Stage Breakdown

```bash
# Check if Rust or Python path is active
curl -s http://localhost:9090/metrics | grep -E "fused_normalizer_enabled|feature_engine_backend"
```

Fused Rust path (`HFT_FUSED_NORMALIZER=1`) combines normalize + LOB + feature into a single zero-copy call.

## Anti-Patterns

These patterns cause latency penalties on the hot path:

| Anti-Pattern | Penalty | Replacement |
|-------------|---------|-------------|
| `datetime.now()` | System call overhead (~1us) | `timebase.now_ns()` (monotonic) |
| `decimal.Decimal` in hot path | Allocation per operation | Scaled int (x10000) |
| `pandas.DataFrame` in loop | Heavy overhead (ms-range) | `numpy` arrays or dict of arrays |
| `print()` | Blocking I/O | `structlog` (async/buffered) |
| `try-except` in tight loop | Stack unwinding cost | Branching / return codes |
| `[x for x in ...]` in tick loop | Heap allocation per tick | Pre-allocated `self.buffer[i] = x` |
| Array of Objects | Pointer chasing, cache misses | Structure of Arrays (numpy/Rust Vec) |
| `json.loads(big)` on main loop | Blocks > 1ms | `orjson` in thread pool |
| `requests.get()` | Blocking I/O | `await client.get()` |
| `time.sleep()` | Blocks event loop | `await asyncio.sleep()` |

## Profiling Tools

### Python Profiling

```bash
# cProfile for function-level timing
python -m cProfile -o profile.out -m hft_platform run sim
# Analyze:
python -c "import pstats; p = pstats.Stats('profile.out'); p.sort_stats('cumulative').print_stats(30)"

# py-spy for live flame graphs (non-intrusive)
py-spy record -o flamegraph.svg --pid $(pgrep -f "hft_platform")

# py-spy top for live view
py-spy top --pid $(pgrep -f "hft_platform")
```

### Rust Profiling

```bash
# Flamegraph for Rust hot path
cd rust_core
cargo flamegraph --bench lob_bench

# Criterion benchmarks
cargo bench
```

### Memory Profiling

```bash
# Check GC activity (should be disabled during trading)
python -c "import gc; print(gc.get_stats())"

# tracemalloc for allocation tracking
python -X tracemalloc=10 -m hft_platform run sim
```

## Latency Realism Guard

Internal microsecond-stage latency does NOT imply executable trading latency.

| Measurement | Typical Value | Source |
|-------------|---------------|--------|
| System internal (normalize+LOB+strategy+risk) | ~10-200 us | Prometheus metrics |
| Shioaji sim API RTT (P50) | ~20 ms | Measured |
| Shioaji sim API RTT (P95) | ~36 ms | Measured |
| Ratio (broker / internal) | ~500x | -- |

### Mandatory Policy

1. Model `place_order`, `update_order`, `cancel_order` latencies separately in research/backtests
2. Use at least P95 latency assumptions for promotion decisions (P99 for stress tests)
3. Record latency assumptions in research artifacts; missing latency profile = non-promotion-ready
4. Treat sub-broker-RTT alpha half-lives as optimistic until validated via shadow/live evidence

## Optimization Priorities

| Priority | Action | Expected Gain |
|----------|--------|---------------|
| 1 | Enable fused Rust path (`HFT_FUSED_NORMALIZER=1`) | 5-10x normalize+LOB |
| 2 | Rust feature engine (`HFT_FEATURE_ENGINE_BACKEND=rust`) | 3-5x feature compute |
| 3 | Pre-allocate all hot path buffers | Eliminate GC pressure |
| 4 | Disable GC during trading hours | Remove stop-the-world pauses |
| 5 | CPU isolation for strategy threads | Reduce tail latency jitter |

## Reference

- Latency baseline: `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`
- Performance rules: `.agent/rules/10-hft-performance.md`
- Rust exports: `src/hft_platform/rust_core` (see CLAUDE.md for full export table)

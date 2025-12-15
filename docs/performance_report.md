# HFT Platform Performance Report

## Executive Summary
**Max Frequency**: ~3,300 Hz (Ticks/Second)
**Latencies**:
- **Feed Ingestion**: < 50 µs
- **Strategy Logic (Python)**: ~300 µs (P99)
- **Architecture Overhead**: ~100 µs

## Benchmark Results
Using `tests/benchmark/latency_test.py` (Simulated Shioaji Feed):

| Metric | Result | Constraint |
|---|---|---|
| **Max Throughput** | **3,300 events/sec** | Global Interpreter Lock (GIL) & Single-threaded Event Loop |
| **Logic Latency** | **303 µs** | Python dynamic dispatch & Strategy overhead |
| **Feed Capacity** | **>20,000 events/sec** | High-performance deque/normalization |

> [!NOTE]
> These tests were run in a simulated environment measuring the *software stack overhead*. Real network latency (broker <-> server) will add 5-50ms significantly dominating the 0.3ms internal latency.

## Shioaji API Assessment
For the specific request of "How fast can we go with Shioaji API?":
1. **API Limit**: Shioaji typically streams ticks at 100-500ms intervals (Snapshot) or ~10-100Hz (Tick Stream).
2. **Platform Headroom**: The platform's **3,300 Hz** capacity is **30x faster** than the typical market data rate (100 Hz).
3. **Bottleneck**: The bottleneck will be the **Network** (Internet/Broker), not this Python platform, for standard strategies.

## Recommendations
1. **For Production**: The current Python stack is sufficient for strategies with holding periods > 1 second.
2. **For HFT (< 10ms)**: Migrating the `on_book` logic to Rust/C++ (via the proposed `hftbacktest` integration or Cython) would reduce internal latency from 300µs -> 5µs.

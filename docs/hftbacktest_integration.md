# Deep Integration Proposal: HftBacktest

## Overview
`hftbacktest` is a high-fidelity simulation framework that accounts for **Order Latency**, **Queue Position**, and **Market Depth** interactions. The current `hft_platform` adapter provides basic connectivity but processes events in a slow Python loop, missing the framework's core performance and fidelity benefits.

This proposal outlines a roadmap to deepen the integration, making `hft_platform` a true "Sim-to-Real" environment where strategies developed for Live trading can be verified with nanosecond-level precision.

## 1. High-Fidelity Data Pipeline
Currently, data sources are disparate (CSV vs Shioaji vs NPZ).
**Goal**: Unified Data Lake.

### A. WAL-to-HBT Converter (`cli.py`)
Implement a converter that transforms the platform's native JSONL WAL (Write-Ahead Log) into `hftbacktest` compatible `.npz` files.
*   **Format**: `[EventFlags, ExchTS, LocalTS, Price, Qty]`
*   **Benefit**: Backtest on the *exact* data stream captured during live storage, preserving native latency signatures (Exchange TS vs Ingest TS).

```python
# Concept
def convert_wal_to_npz(wal_path, output_path):
    # Reads jsonl
    # Extracts timestamps and price updates
    # Writes structured numpy array
```

## 2. Numba-Optimized Strategy Path
`hftbacktest` shines when logic is JIT-compiled.
**Goal**: Sub-microsecond backtest loop.

### A. Hybrid Strategy Class
Extend `BaseStrategy` to verify if a JIT-compiled kernel exists.
*   **Python Path** (Live/Research): Uses `on_lob(dict)`.
*   **Numba Path** (Backtest): Uses `on_lob_numba(float array)`.
*   **Integration**: The Adapter detects `@jit` methods and passes raw NumPy views from `hftbacktest`'s internal HashMap, skipping Dict creation.

```python
# src/hft_platform/strategy/jit_base.py
@numba.jit(nopython=True)
def calc_alpha(lob_array):
    # Fast calculation on raw array
    return (lob_array[0] + lob_array[1]) / 2 # mid
```

## 3. Advanced Latency & Queue Simulation
The current adapter uses `ConstantLatency`. Real HFT requires dynamic modeling.

### A. Feed vs Order Latency
Configure separate latencies for:
*   **Feed**: Time from Exchange -> Strategy (`local_ts - exch_ts` in data).
*   **Order**: Time from Strategy -> Exchange (Network RTT + Gateway).

### B. Queue Position Visibility
Expose `QueuePos` to the strategy for reinforcement learning or analysis.
*   **Feature**: `ctx.get_queue_position(order_id)`
*   **Benefit**: Strategy can cancel orders if they lose queue priority (e.g., "Join if Queue < 50%").

## 4. Post-Trade Analysis (PTA)
Leverage `hftbacktest`'s generic stats output.
**Goal**: Automated PDF/HTML Report.

*   **Metrics**:
    *   **Fill Rate**: Orders Filled / Orders Sent.
    *   **Queue Depletion**: How often orders were cancelled due to price moving away vs filled.
    *   **Latency Breakdown**: Histogram of tick-to-trade latencies.

## 5. Implementation Roadmap

### Phase 1: Data Fidelity (Now)
- [ ] Implement `convert_wal_to_hbt` command.
- [ ] Ensure `Recorder` captures `ExchTS` and `LocalTS` accurately.

### Phase 2: Execution Fidelity
- [ ] Update `HftBacktestAdapter` to use `FeedLatency` from data (replay gap).
- [ ] Expose `QueuePosition` in `StrategyContext`.

### Phase 3: Performance (Optional)
- [ ] Implement Numba-compatible `BaseStrategy` protocols.

---
name: hft-backtester
description: High-fidelity event-driven backtesting using the `hftbacktest` framework. Supports tick-level simulation, latency modeling, queue position simulation, and Numba-accelerated strategies.
tools:
  - run_command
  - write_to_file
  - read_file
---

# HFT Backtester Skill

This skill provides a standard workflow for developing, testing, and analyzing high-frequency trading strategies using the `hftbacktest` library.

## Prerequisite: Environment Setup

The `hftbacktest` library is located in the external repository. You **MUST** add it to your `PYTHONPATH` before running any scripts.

```bash
export PYTHONPATH=$PYTHONPATH:/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest
```

## Core Concepts

*   **Event-Driven**: The simulation progresses event-by-event (ticks, order updates).
*   **Numba JIT**: Strategies **MUST** be implemented as JIT-compiled functions (`@njit`) for performance. Python objects (classes) cannot be easily passed into JIT functions; use `Dict`, `List`, or structured numpy arrays.
*   **Time Unit**: Timestamps are strictly in **nanoseconds**.
*   **Asset Identifying**: Assets are identified by an integer index (`asset_no`) corresponding to their order in the list passed to the backtester.

## End-to-End Workflow

### 1. Data Ingestion & Preparation

Raw data (Binance, Tardis, etc.) must be converted to the `npz` format. Both feed data and initial snapshots are required.

**Standard Format (`data` array in .npz):**
Structured numpy array with named columns: `['ev', 'exch_ts', 'local_ts', 'px', 'qty', ...]`

**Ingestion Script Pattern:**
```python
from hftbacktest.data.utils import binancefutures
# ... download or load raw data ...
# Convert to npz
binancefutures.convert(
    input_files,
    output_filename='data/btcusdt_20240101.npz',
    buffer_size=100_000_000
)
```

**Initial Snapshot:**
Always create an End-Of-Day (EOD) snapshot from the previous day's data to initialize the order book correctly.
```python
from hftbacktest.data.utils import snapshot
snapshot.create_last_snapshot(
    'data/btcusdt_20231231.npz',
    'data/btcusdt_20231231_eod.npz'
)
```

### 2. Strategy Implementation

Strategies are Numba-compiled functions. They contain the main event loop.

**Template:**
```python
from numba import njit
from hftbacktest import HftBacktest, GT, GTX, LIMIT, BUY, SELL

@njit
def my_strategy(hbt):
    # 1. Parameter Setup
    asset_no = 0
    tick_size = hbt.depth(asset_no).tick_size
    
    # 2. State Variables (must be typed for Numba if using containers)
    # from numba.typed import Dict
    
    # 3. Main Loop
    # hbt.elapse(ns) advances time. Returns 0 if interval reached, 1 if order update occurred.
    while hbt.elapse(100_000_000) == 0: # 100ms interval
        
        # 4. Data Access
        depth = hbt.depth(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        
        # 5. Order Management
        # Clean up finished orders
        hbt.clear_inactive_orders(asset_no)
        
        # Place Order
        # Args: asset_no, order_id, price, qty, time_in_force, order_type, wait_response
        order_id = 100 
        hbt.submit_buy_order(asset_no, order_id, best_bid, 1.0, GTX, LIMIT, False)
        
        # Cancel Order
        # hbt.cancel(asset_no, order_id, False)
        
    return True
```

### 3. Simulation Configuration & Execution

Use `HashMapMarketDepthBacktest` for general purpose or `ROIVectorMarketDepthBacktest` for specific ROI-based optimizations.

**Simulation Script Pattern:**
```python
import numpy as np
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from strategy import my_strategy

def run():
    # 1. Define Asset
    asset = (
        BacktestAsset()
            .data(['data/btcusdt_20240101.npz'])
            .initial_snapshot('data/btcusdt_20231231_eod.npz')
            .linear_asset(1.0) # Quantity multiplier
            # --- Latency Model ---
            # fixed: .constant_latency(10_000_000, 10_000_000) # 10ms
            # file-based: .intp_order_latency(np.load('latency.npz')['data'])
            .constant_latency(10_000_000, 10_000_000)
            # --- Queue Model ---
            # .risk_adverse_queue_model() or .power_prob_queue_model(3)
            .risk_adverse_queue_model() 
            # --- Fees ---
            # Maker / Taker
            .trading_value_fee_model(-0.00005, 0.0007) 
            .tick_size(0.1)
            .lot_size(0.001)
    )

    # 2. Initialize Backtester
    hbt = HashMapMarketDepthBacktest([asset])

    # 3. Run Strategy
    # Pass strategy function
    my_strategy(hbt)

    # 4. Cleanup
    hbt.close()
```

### 4. Analysis & Reporting

Use `Recorder` within the strategy or post-process data.

**Recording Stats:**
Inside strategy (pass `recorder` as arg):
```python
# In loop
recorder.record(hbt)
```

**Post-Processing:**
```python
from hftbacktest.stats import LinearAssetRecord
stats = np.load('stats.npz')
# Use polars/pandas to analyze 'equity', 'drawdown', 'sr'
```

## Best Practices

1.  **Floating Point Precision**: Prices are floats. Use `round(price / tick_size) * tick_size` to align prices to ticks and avoid floating point errors.
2.  **Order IDs**: Manage order IDs carefully. They must be unique per asset.
3.  **Local vs Exchange Timestamp**: Be aware of the difference. Strategies run on local time, but fill logic depends on exchange time flow.

5.  **Jitclass for State**: Use `numba.experimental.jitclass` to encapsulate strategy state. This avoids Python dictionary lookups in the hot loop and keeps memory strictly typed.
6.  **Recursive Calculation**: For path-dependent indicators (like Hawkes Processes or EWMA), always prefer recursive formulations ($O(1)$) over history-based summation ($O(N)$) to satisfy latency constraints.

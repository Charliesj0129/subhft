# hftbacktest v2.4.3 API Reference

Source: Python bindings + Rust source code

## BacktestAsset (Builder Pattern)

```python
from hftbacktest import BacktestAsset

asset = (
    BacktestAsset()
    .data(["data/TMFD6_20260301.npz", "data/TMFD6_20260302.npz"])
    .initial_snapshot(snapshot_array)        # optional: starting book state
    .linear_asset(1.0)                       # contract_size (futures: 1.0)
    .tick_size(1.0)                          # minimum price increment
    .lot_size(1.0)                           # minimum order size
    .no_partial_fill_exchange()              # or .partial_fill_exchange()
    .power_prob_queue_model(1.0)             # queue model + parameter
    .constant_order_latency(36_000_000, 36_000_000)  # entry_ns, response_ns
    .flat_per_trade_fee_model(2.0, 2.0)      # maker_fee, taker_fee
    .roi_lb(0.0)                             # ROI price lower bound
    .roi_ub(30000.0)                         # ROI price upper bound
    .last_trades_capacity(10000)             # trade buffer size
)
```

## Backtester Construction

```python
from hftbacktest import ROIVectorMarketDepthBacktest, HashMapMarketDepthBacktest

# For known price range (faster, vector-based):
hbt = ROIVectorMarketDepthBacktest([asset])

# For unknown/wide price range (hash-based):
hbt = HashMapMarketDepthBacktest([asset])
```

## Main Loop Methods

```python
# Time-based stepping (duration in nanoseconds)
while hbt.elapse(100_000_000) == 0:  # 100ms steps
    pass

# Event-based stepping (react to every feed update)
while hbt.wait_next_feed(True, 0) == 0:  # include_order_resp, timeout_ns
    pass
```

## Market Depth Access

```python
depth = hbt.depth(0)  # asset_no = 0

depth.best_bid          # float: best bid price
depth.best_ask          # float: best ask price
depth.best_bid_tick     # int: best bid in ticks
depth.best_ask_tick     # int: best ask in ticks
depth.best_bid_qty      # float: quantity at best bid
depth.best_ask_qty      # float: quantity at best ask
depth.tick_size         # float: tick size
depth.lot_size          # float: lot size
depth.mid_price         # float: (best_bid + best_ask) / 2

# Quantity at specific price level
depth.bid_qty_at_tick(price_tick)
depth.ask_qty_at_tick(price_tick)

# Full book snapshot
snapshot = depth.snapshot()  # numpy structured array
```

## Order Management

```python
# Submit orders
hbt.submit_buy_order(
    asset_no=0,
    order_id=1,            # unique order ID (your tracking)
    price=best_bid,        # limit price
    qty=1.0,               # order quantity
    time_in_force=GTC,     # GTC, GTX (post-only), FOK, IOC
    order_type=LIMIT,      # LIMIT or MARKET
    wait=False,            # True = block until ack
)

hbt.submit_sell_order(0, 2, best_ask, 1.0, GTC, LIMIT, False)

# Modify order (price/qty change)
hbt.modify(0, order_id, new_price, new_qty, wait=False)

# Cancel order
hbt.cancel(0, order_id, wait=False)

# Clear filled/cancelled orders from cache
hbt.clear_inactive_orders(0)
```

## Order Status

```python
from hftbacktest import NONE, NEW, FILLED, CANCELED, PARTIALLY_FILLED, EXPIRED
from hftbacktest import GTC, GTX, FOK, IOC
from hftbacktest import BUY, SELL, LIMIT, MARKET

orders = hbt.orders(0)  # dict[int, Order]
order = orders[order_id]

order.status          # NONE, NEW, FILLED, CANCELED, PARTIALLY_FILLED, EXPIRED
order.req             # NONE, NEW, CANCELED (pending request)
order.price           # limit price
order.exec_price      # execution price
order.qty             # original quantity
order.leaves_qty      # remaining unfilled quantity
order.exec_qty        # executed quantity
order.side            # BUY or SELL
order.maker           # bool: filled as maker?
order.cancellable     # bool: can be cancelled?
order.exch_timestamp  # exchange processing time (ns)
order.local_timestamp # local request time (ns)
```

## Position & State

```python
position = hbt.position(0)        # current position (float)
state = hbt.state_values(0)       # StateValues object

state.position         # open position
state.balance          # cash balance
state.fee              # accumulated fees
state.num_trades       # total number of fills
state.trading_volume   # total quantity traded
state.trading_value    # total notional value traded
```

## Last Trades Access

```python
trades = hbt.last_trades(0)  # numpy array of recent trades
# Each trade has: ev, exch_ts, local_ts, px, qty, order_id, ival, fval
```

## Data Format (event_dtype)

```python
from hftbacktest import event_dtype

# numpy structured array dtype:
# ev:       uint8  — event type flags
# exch_ts:  int64  — exchange timestamp (nanoseconds)
# local_ts: int64  — local timestamp (nanoseconds)
# px:       float64 — price
# qty:      float64 — quantity (negative = cancellation for depth events)
# order_id: uint64 — order ID (for L3 data)
# ival:     int64  — auxiliary integer
# fval:     float64 — auxiliary float

# Event type flags (bitwise OR):
DEPTH_EVENT          = 1       # Book depth update
TRADE_EVENT          = 2       # Trade execution
DEPTH_SNAPSHOT_EVENT = 4       # Full book snapshot
ADD_ORDER_EVENT      = 10      # L3: new order
CANCEL_ORDER_EVENT   = 11      # L3: cancel
MODIFY_ORDER_EVENT   = 12      # L3: modify
FILL_EVENT           = 13      # L3: fill

EXCH_EVENT = 1 << 31   # Timestamp is exchange time
LOCAL_EVENT = 1 << 30   # Timestamp is local time
BUY_EVENT  = 1 << 29   # Buy side / bid
SELL_EVENT = 1 << 28   # Sell side / ask
```

## Queue Model Options

```python
# Conservative: only trades advance queue position
asset.risk_adverse_queue_model()

# Probability-based with power function:
asset.power_prob_queue_model(n)    # prob = back^n / (back^n + front^n)
asset.power_prob_queue_model2(n)   # prob = back^n / (back + front)^n
asset.power_prob_queue_model3(n)   # variant

# Probability-based with log function:
asset.log_prob_queue_model()       # prob = log(1+back) / (log(1+back) + log(1+front))
asset.log_prob_queue_model2()      # prob = log(1+back) / log(1+back+front)

# L3 FIFO (requires order-by-order data):
asset.l3_fifo_queue_model()
```

## Exchange Models

```python
# No partial fills (default, conservative):
asset.no_partial_fill_exchange()

# With partial fills (supports FOK, IOC, Market orders):
asset.partial_fill_exchange()
```

## Latency Models

```python
# Fixed latency:
asset.constant_order_latency(
    entry_latency_ns,     # request → exchange (nanoseconds)
    response_latency_ns,  # exchange → response
)

# Interpolated from historical data:
asset.intp_order_latency(
    latency_data,         # .npz files with (req_ts, exch_ts, resp_ts, _pad) records
    latency_offset=0,     # optional fixed offset
)
```

## Fee Models

```python
# Fixed per trade:
asset.flat_per_trade_fee_model(maker_fee, taker_fee)

# Based on quantity:
asset.trading_qty_fee_model(maker_fee, taker_fee)

# Based on notional value:
asset.trading_value_fee_model(maker_fee_rate, taker_fee_rate)
# fee = rate * price * qty * contract_size
```

## Asset Types

```python
# Linear (spot, linear futures):
asset.linear_asset(contract_size)  # equity = balance + size * pos * price - fee

# Inverse (inverse perpetuals):
asset.inverse_asset(contract_size)  # equity = -balance - size * pos / price - fee
```

## Statistics & Recording

```python
from hftbacktest import Recorder
from hftbacktest.stats import LinearAssetRecord

recorder = Recorder(1)  # 1 asset
recorder.record(hbt)    # call each step

# After backtest:
equity, trade = recorder.get(0)  # asset_no = 0
equity_values = LinearAssetRecord(equity)  # structured access

from hftbacktest.stats import compute_metrics
metrics = compute_metrics(equity, trade, tick_size)
# Returns: Sharpe, Sortino, max_drawdown, etc.
```

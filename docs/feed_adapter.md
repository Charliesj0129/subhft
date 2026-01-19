# Feed Adapter Guide

## Overview
The Feed Adapter connects Shioaji (Sinotrade) Websocket API to the HFT Event Bus.
It manages:
- **Session**: Login, Token Refresh.
- **Resilience**: Heartbeat monitoring, Automatic Reconnection, Snapshot bootstrapping.
- **Normalization**: Converting raw Shioaji dicts to standardized `Tick`/`BidAsk` events.
- **Discipline**: Pinned consumer thread usage to minimize jitter.

## Configuration
Configure symbols in `config/symbols.yaml`:

```yaml
symbols:
  - code: "2330"
    exchange: "TSE"
  - code: "2317"
    exchange: "TSE"
```

## CLI Usage
Use the CLI to check connection status or verify config:

```bash
# Check Config
python -m hft_platform.feed_adapter.cli status --config config/symbols.yaml

# Verify Login (Requires credentials in env)
python -m hft_platform.feed_adapter.cli verify-login
```

## Metrics
The adapter exposes Prometheus metrics at `:9090/metrics`:
- `feed_events_total`: Count of incoming ticks/quotes.
- `feed_state`: Gauge (0=INIT, 1=CONNECTED, ...).
- `bus_overflow_total`: Number of drops if consumer lags.

## Architecture
1. **Shioaji Thread**: Callback -> `raw_queue.put()` (Thread-safe).
2. **Consumer Task**: `raw_queue.get()` -> Normalize -> Update LOB -> Publish to Bus.
3. **Monitor Task**: Checks `last_event_ts`. If gap > 5s -> Reconnect.

## LOB Engine (Phase 11)

The **LOB Engine** (`lob_engine.py`) maintains an in-memory Limit Order Book for each symbol, bootstrapped from snapshots and updated via incremental feeds.

### BookState Schema
Each symbol has a `BookState` object containing:
- **Ladders**: `bids`/`asks` (List of Top-5 levels: `{price, volume}`).
- **Metadata**: `exch_ts`, `local_ts`, `version`, `degraded` (flag).
- **Derived Features**:
    - `mid_price`: `(best_bid + best_ask) / 2`
    - `spread`: `best_ask - best_bid`
    - `imbalance`: `(bid_vol - ask_vol) / (bid_vol + ask_vol)` (Top-1)
    - `bid_depth_total` / `ask_depth_total`: Sum of volumes in top 5 levels.

### Strategy Access
Strategies consume LOB data through events:
- `BidAskEvent` for book updates.
- `LOBStatsEvent` for derived stats (mid/spread/imbalance).

### Observability
- `lob_updates_total`: Counter by type (BidAsk/Tick).
- `lob_snapshots_total`: Counter for full resets.

## Normalization Schema
The normalizer converts raw payloads into consistent events.

### Tick
```json
{
  "type": "Tick",
  "symbol": "2330",
  "exch_ts": 1678888888000000,
  "local_ts": 1678888888000500,
  "price": 5000000,  // 500.0 * 10000
  "volume": 15
}
```

### BidAsk / Snapshot
```json
{
  "type": "BidAsk", // or Snapshot
  "symbol": "2330",
  "bids": [{"price": 4995000, "volume": 10}, ...],
  "asks": [{"price": 5000000, "volume": 5}, ...]
}
```
All prices are scaled by `10000`.

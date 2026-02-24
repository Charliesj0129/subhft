# feed_adapter

## Purpose

Connect to market data sources, normalize raw payloads into internal events, and maintain Level 2 order book state.

## Key Files

| File                             | Key Class                                | Lines                                                      |
| -------------------------------- | ---------------------------------------- | ---------------------------------------------------------- |
| `feed_adapter/shioaji_client.py` | `ShioajiClient`                          | 2178 lines. Broker API: login, subscribe, order, callbacks |
| `feed_adapter/normalizer.py`     | `MarketDataNormalizer`, `SymbolMetadata` | 933 lines. Raw→TickEvent/BidAskEvent                       |
| `feed_adapter/lob_engine.py`     | `LOBEngine`                              | 554 lines. L2 book state → LOBStatsEvent                   |

## Data Flow

```
Shioaji callback (broker thread)
→ _try_fast_extract_callback_payload()  # Handle API signature drift
→ loop.call_soon_threadsafe(_enqueue_raw)
→ raw_queue (bounded asyncio.Queue)
→ normalizer.normalize_tick() or normalize_bidask()  # Rust fast path
→ LOBEngine.process_event()  # Rust fast path (compute_book_stats)
→ RingBufferBus.publish_nowait()
```

## Rust Fast Paths

| Function                              | Python Fallback      | Speedup |
| ------------------------------------- | -------------------- | ------- |
| `normalize_bidask_tuple_with_synth()` | Python loop + lists  | ~100x   |
| `scale_book_pair_stats()`             | Python dict building | ~50x    |
| `compute_book_stats()`                | Python numpy/loops   | ~50x    |

## SymbolMetadata

- Loaded from `config/symbols.yaml` (or `config/base/symbols.yaml`).
- Per-symbol: `price_scale`, `tick_size`, `tags[]`, `exchange`.
- **Hot-reload**: `reload_if_changed()` watches file mtime (called by MarketDataService monitor).
- **Tag-based subscription**: Strategies use `tag:futures|etf` to dynamically resolve symbols.

## LOBEngine

- **Per-symbol BookState**: Full L2 order book maintained.
- **L1 Fast Path**: `get_l1_scaled(symbol)` → `(ts_ns, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth)` — zero dict allocation.
- **Metrics Worker**: Periodic emit of book depth/spread metrics. Started from event loop.

## Configuration

| Variable                | Default               | Purpose                       |
| ----------------------- | --------------------- | ----------------------------- |
| `SYMBOLS_CONFIG`        | `config/symbols.yaml` | Symbols config file path      |
| `HFT_MD_LOG_RAW`        | `0`                   | Log raw market data callbacks |
| `HFT_MD_LOG_EVERY`      | `1000`                | Log every N-th raw event      |
| `HFT_MD_LOG_NORMALIZED` | `0`                   | Log normalized events         |

## Gotchas

- Shioaji callback signature drifts across versions — `_try_fast_extract_callback_payload()` handles multiple shapes.
- Normalizer caches `price_scale` per symbol. After `symbols.yaml` reload, cache is updated via `SymbolMetadata`.
- One-sided LOB snapshots (only bid or only ask) can arrive pre-market. Guards exist in normalizer.
- LOBEngine's `get_l1_scaled()` returns a **tuple**, not a dict. Unpack positionally.

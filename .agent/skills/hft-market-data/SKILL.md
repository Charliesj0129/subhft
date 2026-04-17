<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->
---
name: hft-market-data
description: Use when working on market data ingestion, normalization, LOB engine, broker quote callbacks, tick dispatch, reconnect, or any code in feed_adapter/, normalizer, lob_engine.
---

# HFT Market Data Plane

Use this skill for `feed_adapter/` (47 files) and `feature/` (8 files). This is the hottest path in the system — broker callback to event bus.

## Data Flow (complete)

```text
Exchange (Shioaji/Fubon SDK)
  -> [Broker Callback Thread]
    -> QuoteRuntime._on_tick_impl()
      -> TickDispatcher.enqueue_tick()        [queue.Queue(8192) or deque+Event]
        -> [Worker Thread]
          -> process_tick_fn()                [ShioajiClient._process_tick]
            -> MarketDataService.on_tick()
              -> Normalizer.normalize_tick()   [Python or Rust path]
                -> LOBEngine.process_event()   [Python numpy or Rust]
                  -> FeatureEngine.update()    [27 features v3]
                    -> RingBufferBus.emit()    [3 modes]
```

**Fused path** (`HFT_FUSED_NORMALIZER=1`):
```text
Raw payload -> RustNormalizerLobFused (single FFI call)
  = normalize + LOB update + stats in one Rust function (20-30x faster)
```

## Module Map

### feed_adapter/ root (7 files)
| File | Size | Public API | Purpose |
| --- | --- | --- | --- |
| `normalizer.py` | 51KB | `MarketDataNormalizer`, `SymbolMetadata` | Raw -> TickEvent/BidAskEvent (scaled int x10000) |
| `lob_engine.py` | 27KB | `LOBEngine`, `BookState` | Per-symbol LOB state + stats, numpy fast-path |
| `protocol.py` | 3KB | `BrokerClientProtocol`, `BrokerOrderCodec` | Broker abstraction (runtime_checkable) |
| `broker_registry.py` | 2KB | `register_broker()`, `get_broker_factory()` | Plugin-style broker factory |
| `contract_fetcher.py` | 5KB | `fetch_all_contracts()` | Contract metadata fetch |
| `subscription_state.py` | 11KB | `SubscriptionStateData` | Persistent subscription state (crash recovery) |
| `shioaji_client.py` | 1KB | (re-export) | Backward compat shim |

### Shioaji sub-package (20 files)
| File | Size | Purpose |
| --- | --- | --- |
| `client.py` | 44KB | Core SDK wrapper, holds all sub-runtimes |
| `facade.py` | 8KB | Composition: 9 sub-runtimes -> BrokerClientProtocol |
| `quote_runtime.py` | 34KB | WebSocket quote management + watchdog |
| `tick_dispatcher.py` | 16KB | Async queue + worker thread (decouple broker thread) |
| `session_runtime.py` | 18KB | Login/CA cert/reconnect + exponential backoff |
| `subscription_manager.py` | 11KB | 2.5s cooldown guard |
| `reconnect_orchestrator.py` | 12KB | Stall/schema-mismatch -> resubscribe + reconnect |
| `router.py` | 15KB | Multi-client global dispatch (CLIENT_REGISTRY) |
| `contracts_runtime.py` | 27KB | Contract caching |
| `order_gateway.py` | 16KB | Order submission/cancellation |
| `account_gateway.py` | 14KB | Position/inventory queries |
| `quote_connection_pool.py` | — | WebSocket connection pooling |
| `order_codec.py` | — | Side/TIF -> SDK enum |
| `historical/market_info/scanner_gateway` | — | Historical data, market info, scanning |
| `_config.py`, `_infra.py`, `_metrics.py` | — | Internal config/locks/metrics |

### Fubon sub-package (14 files)
Same structure. Key difference: 10s cooldown (vs Shioaji 2.5s), pre-allocated reusable buffers.

### feature/ (8 files)
| File | Size | Purpose |
| --- | --- | --- |
| `engine.py` | 38KB | FeatureEngine: 27 features (v3), per-symbol state + EMA accumulators |
| `registry.py` | 10KB | Feature set definitions (v1:16, v2:22, v3:27) |
| `kernel.py` | 11KB | Python reference kernel (OFI, EMA, spread, imbalance) |
| `burst_detector.py` | 9KB | Tick intensity surge detection (Christensen 2024) |
| `profile.py` | 6KB | A/B test parameter templates |
| `boundary.py` | — | FeatureUpdateEvent -> typed frame |
| `compat.py` | — | Version backward compat |
| `rollout.py` | — | Gradual feature rollout |

## Rust Acceleration

| Component | Env | Speedup | Fallback |
| --- | --- | --- | --- |
| normalize_tick | `HFT_RUST_ACCEL=1` (default) | 50-100x | Python dict parse |
| normalize_bidask | `HFT_RUST_ACCEL=1` | 50-100x | Python L5 scaling |
| compute_book_stats | `HFT_LOB_RUST_BOOKSTATE=1` (default) | 10-20x | numpy summation |
| Fused normalizer+LOB | `HFT_FUSED_NORMALIZER=1` | 20-30x | 2-step pipeline |
| Feature kernel | `HFT_FEATURE_ENGINE_BACKEND=rust` | ~10x | Python kernel |
| Event bus typed rings | `HFT_BUS_MODE=rust_typed` | 5-10x | Python deque |

Force flags: `HFT_RUST_ACCEL=0` (disable all), `HFT_RUST_FORCE=1` (fail if unavailable)

## Thread Model

| Component | Thread | Sync |
| --- | --- | --- |
| Broker callback | Shioaji/Fubon SDK thread | No sync needed |
| TickDispatcher | Broker (enqueue) + Worker (dequeue) | queue.Queue or deque+Event |
| Normalizer | Worker thread | Stateless (except sequence counter) |
| LOBEngine | Worker thread | Optional per-symbol Lock (default off) |
| FeatureEngine | Worker thread | No lock (per-symbol cache) |
| EventBus | Emitter + async subscribers | Ring buffer internals |

## Common Fix Patterns (from git history)

| Pattern | Fix |
| --- | --- |
| IEEE 754 truncation in Python fallback | Use `round()` not `int()` for price scaling |
| Shared mutable LOB stats | Per-tick allocation (not shared reference) |
| LOBStats tuple format mismatch | Prepend lobstats tag for runner guard |
| Metrics label explosion | Cardinality guard: max 200 symbols per metric |
| Fused bypass redundant recompute | Skip LOBEngine if fused normalizer already populated stats |
| Feature autocovariance wrong index | Correct ring buffer indexing + warmup counter |

## Cardinality Guards

- **LOBEngine**: `_metrics_max_label_symbols` = 200 (Prometheus label cap)
- **FeatureEngine**: Same 200 symbol cap
- **LOBEngine max books**: bounded by available memory (no explicit cap, but monitored)

## Quality Flags (FeatureEngine)

| Flag | Meaning |
| --- | --- |
| `QUALITY_FLAG_GAP` | Missing updates between events |
| `QUALITY_FLAG_STATE_RESET` | Kernel was reset |
| `QUALITY_FLAG_STALE_INPUT` | Input timestamp too old |
| `QUALITY_FLAG_OUT_OF_ORDER` | Sequence violation |
| `QUALITY_FLAG_PARTIAL` | Incomplete book (one-sided) |

## Key Environment Variables

| Variable | Default | Effect |
| --- | --- | --- |
| `HFT_BROKER` | `shioaji` | Broker selection |
| `HFT_RUST_ACCEL` | `1` | Enable Rust acceleration |
| `HFT_FUSED_NORMALIZER` | `0` | Enable fused Rust pipeline |
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | Enable FeatureEngine |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | `rust` for Rust kernel |
| `HFT_BUS_MODE` | `python` | Event bus mode |
| `HFT_LOB_LOCKS` | `0` | Per-symbol locking |
| `HFT_QUOTE_VERSION` | `auto` | Shioaji quote protocol |
| `HFT_TICK_RING_BUFFER` | `0` | Use deque vs queue.Queue for tick dispatch |

## Testing

```bash
make test-file FILE=tests/unit/test_normalizer.py
make test-file FILE=tests/unit/test_lob_engine.py
make test-file FILE=tests/unit/test_feature_engine.py
make test-file FILE=tests/unit/test_market_data_service_behavior.py
make hotpath-profile    # Latency: normalizer -> LOB -> feature -> strategy -> risk
```

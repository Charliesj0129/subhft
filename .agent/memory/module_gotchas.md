# Module Gotchas & Patterns

> **Purpose**: Agent-specific knowledge about non-obvious behaviors in each module. Read BEFORE modifying any module.

## strategy/runner.py (515 lines)

- **Circuit Breaker FSM**: 3-state (normal→degraded→halted) per strategy. Threshold: `HFT_STRATEGY_CIRCUIT_THRESHOLD` (default 10). Cooldown: `HFT_STRATEGY_CIRCUIT_COOLDOWN_S` (default 60s). Recovery requires N/2 consecutive successes.
- **Typed Intent Fast Path**: When `HFT_TYPED_INTENT_CHANNEL=1` (default), intents are emitted as tuples (`("typed_intent_v1", ...)`) instead of `OrderIntent` objects — **zero allocation**. Gateway recognizes `"typed_intent_v1"` tag.
- **Metrics Batching**: Metrics are NOT emitted per-event. `HFT_STRATEGY_METRICS_BATCH` controls how often (default varies by `HFT_OBS_POLICY`). Pending counts are flushed on shutdown.
- **Position Cache**: `_positions_dirty` flag + dict snapshot pattern to avoid dict-changed-during-iteration from concurrent broker threads. Cache key format is `"pos:{strat_id}:{symbol}"`.
- **Symbol Resolution**: Strategies can use `tag:futures|etf` syntax to dynamically subscribe via `SymbolMetadata.symbols_for_tags()`.
- **GOTCHA**: `_strat_executors` is a cached tuple list. If tests replace `self.strategies`, call `_rebuild_executors()` or the old cached contexts are used.

## services/market_data.py (966 lines)

- **FeedState FSM**: INIT→CONNECTING→SNAPSHOTTING→CONNECTED→DISCONNECTED→RECOVERING.
- **Reconnect Logic**: Three tiers: resubscribe (15s gap) → reconnect (60s gap) → force reconnect (300s gap). Cooldown prevents rapid reconnects (`HFT_MD_RECONNECT_COOLDOWN_S`).
- **Session Rollover**: `HFT_RECONNECT_DAYS` + `HFT_RECONNECT_HOURS` trigger scheduled reconnects. `_last_rollover_reconnect_date` prevents double reconnect.
- **Per-Symbol Watchdog**: `_watchdog_loop()` checks each symbol's last tick time. If ≥2 symbols stale, triggers resubscribe. Threshold relaxed during market open grace period (`HFT_MARKET_OPEN_GRACE_S`).
- **Recorder Degradation**: If recorder queue overflows, market_data enters degraded mode and drops records (not ticks). Recovers when queue drops below 50%.
- **Backpressure**: `_enqueue_raw()` uses `put_nowait()` with drop counter. Metrics: `raw_queue_dropped_total`, `raw_queue_depth`.
- **Callback Parsing**: `_try_fast_extract_callback_payload()` handles Shioaji API signature drift across versions. Always `_unwrap_md()` to handle nested `{"tick": {...}}` payloads.
- **GOTCHA**: `_on_shioaji_event` runs in a **broker thread**, not the event loop. Must use `loop.call_soon_threadsafe()` to enqueue.
- **P0-2 Optimization**: Per-symbol tick timestamp is updated inline (CPython dict assignment is GIL-atomic) instead of creating an asyncio task per tick.
- **Crash Detector Injection**: `crash_detector: Callable` is an optional `__init__` dependency. Shioaji injects `detect_crash_signature`; non-Shioaji brokers may leave it unset.
- **Renamed Broker Hooks**: Multi-broker support renamed `_on_shioaji_event`→`_on_broker_event` and `_record_shioaji_crash_signature`→`_record_broker_crash_signature`. Metric name is now `broker_crash_signature_total`.

## recorder/worker.py (296 lines)

- **6 Batcher Topics**: `market_data` → `hft.market_data`, `orders` → `hft.orders`, `fills` → `hft.trades`, `risk_log` → `hft.logs`, `backtest_runs` → `hft.backtest_runs`, `latency_spans` → `hft.latency_spans`.
- **WAL-First Mode**: When `HFT_RECORDER_MODE=wal_first`, data goes to WAL files only (no ClickHouse). Use `wal-loader` container to replay later.
- **Schema Extractors (CC-5)**: `_extract_market_data()`, `_extract_order()`, `_extract_fill()` bypass generic `serialize()` for speed. Controlled by `HFT_BATCHER_SCHEMA_EXTRACT`.
- **Memory Guard**: `GlobalMemoryGuard` tracks total buffered rows across all batchers to prevent OOM.
- **GOTCHA**: `recover_wal()` is skipped in WAL-first mode. In direct mode it replays `.wal/` files on startup.

## gateway/service.py (256 lines)

- **7-Step Pipeline**: dedup→policy→exposure→risk→command→commit→dispatch. All synchronous except queue I/O.
- **Typed Frame Support**: If `risk_engine` has `typed_frame_view()`, gateway deserializes typed intent tuples lazily (only after passing dedup+policy+exposure).
- **Exposure Release**: On risk rejection, exposure is released (`_exposure.release_exposure()`) to prevent phantom exposure buildup.
- **GOTCHA**: `_order_adapter._api_queue.put_nowait(cmd)` — direct access to adapter's internal queue. If queue is full, intent is dropped and dedup entry committed as rejected.
- **GOTCHA**: Metrics use deferred imports (`from hft_platform.observability.metrics import MetricsRegistry`) to avoid circular imports. Never change to top-level imports.

## risk/engine.py + storm_guard.py

- **StormGuardFSM**: NORMAL(0)→WARM(1)→STORM(2)→HALT(3). HALT stops all order flow. Triggers: latency spikes, excessive gaps, manual. State propagated via bus and OrderCommand.
- **Validators are stateless**: `PriceBandValidator`, `MaxNotionalValidator` each check against config. Adding a validator = implement `validate(intent)` → `(bool, str)`.
- **GOTCHA**: Risk config path comes from `settings["risk_config"]` (YAML file path), not inline config.

## order/adapter.py (598 lines)

- **Dead Letter Queue (DLQ)**: Failed orders go to `_dead_letters` list. Use CLI `hft dlq` to inspect.
- **Circuit Breaker**: Separate from strategy circuit breaker. Opens after N consecutive broker API failures.
- **Rate Limiter**: Sliding window per broker session. `HFT_ORDER_RATE_LIMIT` env var.
- **Broker ID Registration**: `_broker_id_map` tracks ordno↔intent_id mapping for execution router lookup.
- **GOTCHA**: `HFT_ORDER_MODE=sim` makes adapter return fake fills instead of calling broker API. Always check mode before debugging "orders not going through".

## feed_adapter/normalizer.py (933 lines)

- **Rust Fast Path**: `normalize_bidask_tuple_with_synth()` or `normalize_bidask_tuple_np()` from `rust_core.fast_lob`. Falls back to Python if Rust unavailable.
- **SymbolMetadata**: Loaded from `config/symbols.yaml`. Contains `price_scale`, `tick_size`, `tags[]` per symbol. Supports hot-reload via `reload_if_changed()`.
- **GOTCHA**: Normalizer caches `price_scale` lookups. If symbols.yaml changes, `SymbolMetadata.reload_if_changed()` must be called (market_data_service handles this).
- **One-sided LOB**: Snapshots can arrive with only bid or only ask side populated. Guard each side independently instead of assuming a full book.
- **NormalizerFieldMap**: Broker-specific field names live in a frozen dataclass. Keep `_is_default_map=True` for Shioaji to preserve Rust fast paths.

## feed_adapter/shioaji_client.py

- **WebSocket Thread**: All Shioaji callbacks run in a broker-owned thread, not the asyncio event loop. Use `loop.call_soon_threadsafe()` to hand off work safely.
- **Quote Version**: `HFT_QUOTE_VERSION=auto` probes SDK capabilities at runtime. Pin the version to avoid repeated probe overhead when debugging login/session issues.
- **Session Refresh**: Background token renewal is blocking I/O. Never call `sdk.renew_token()` from the asyncio loop.

## feed_adapter/lob_engine.py (554 lines)

- **BookState per symbol**: Maintains full L2 order book. Rust `LimitOrderBook` used if available.
- **L1 Fast Path**: `get_l1_scaled(symbol)` returns tuple `(ts_ns, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth)` — no dict allocation.
- **Metrics Worker**: `start_metrics_worker()` runs a periodic loop to emit book depth/spread metrics. Must be started from the event loop.

## execution/positions.py (280 lines)

- **Integer Arithmetic Only**: PnL, avg_price, net_qty all use scaled ints. NO float anywhere.
- **Dual Implementation**: `on_fill()` (sync) for broker thread callbacks, `on_fill_async()` for event loop. Both update the same state.
- **RustPositionTracker**: Optional replacement from `rust_core.RustPositionTracker`. Same API, ~10x faster.

## config/loader.py (165 lines)

- **Priority Chain**: Base YAML → Env YAML → settings.py → Env Vars → CLI. Later overrides earlier.
- **settings.py**: Lives at `config/settings.py`. Can define `get_settings()` function or top-level UPPERCASE vars.
- **GOTCHA**: `load_settings()` returns `(settings, applied_defaults)` tuple, not just settings.
- **GOTCHA**: Mode resolution logic syncs `HFT_MODE` env var with `settings["mode"]`. If both set, env var wins.

## feed_adapter/fubon/client.py — Fubon TradeAPI Client

- **SDK**: `fubon_neo` package; conditional import (may not be installed)
- **Auth**: API Key + Password (no certificate), env vars `HFT_FUBON_API_KEY`, `HFT_FUBON_PASSWORD`
- **Transport**: HTTP REST for orders, WebSocket for market data (vs Shioaji's proprietary callbacks)
- **Price scaling**: Fubon returns float prices — MUST scale to int x10000 at ingestion boundary
- **Custom field**: `user_def` (32 chars max, vs Shioaji's `custom_field` 6 chars)
- **Batch orders**: Supported — can place/cancel/amend multiple orders in one API call
- **Rate limits**: Different from Shioaji — check `config/base/brokers/fubon.yaml`
- **Gotcha**: WebSocket reconnect logic differs from Shioaji quote watchdog — separate implementation needed

## feed_adapter/broker_registry.py

- **Module-Level Registry**: `_BROKER_REGISTRY` is populated by broker package import side effects. Import order matters during bootstrap.
- **Factory Lookup**: `get_broker_factory(name)` raises `ValueError` for unknown or unavailable brokers. Catch it at the boundary and emit a broker-specific message.
- **Default Broker**: `HFT_BROKER` defaults to `"shioaji"` when unset, so tests and bootstrap paths will silently select Shioaji unless overridden.
- **GOTCHA**: If a broker package import fails because its SDK is missing, registration is skipped and the failure only surfaces later at factory lookup time.

## feed_adapter/fubon/ (broker package)

- **SDK Import Guard**: Guard every `import fubon_neo` with `try/except ImportError`; the SDK is delivered as a platform-specific wheel and is not guaranteed to exist in CI/dev.
- **WebSocket Thread**: Fubon callbacks run in a broker thread just like Shioaji. Always bridge into the loop with `loop.call_soon_threadsafe()`.
- **Pre-allocated Buffers**: Quote/runtime handlers reuse fixed numpy buffers for 5-level books. Do not allocate fresh arrays per message.
- **Price Conversion**: Fubon price payloads often arrive as strings such as `"523.00"`. Convert with `Decimal(str) * 10000`, never `float()`.
- **Book Format**: Incoming levels arrive as `bids[{price, size}]` / `asks[{price, size}]`. Flatten to SoA arrays immediately to preserve cache-friendly downstream handling.
- **Account Object**: `sdk.login()` returns an `accounts` wrapper whose primary account is usually `accounts.data[0]`, not the wrapper itself.
- **Response Unwrapping**: Many SDK calls return wrapped payloads. Use `_unwrap_list()` / `_unwrap_scalar()` helpers rather than reaching through response objects ad hoc.
- **GOTCHA**: Dependency metadata uses `fubon-neo` while the import name is `fubon_neo`; this mismatch is easy to miss during packaging/debugging.

# Module Gotchas

Non-obvious behaviors, edge cases, and traps discovered while working with this codebase.
Format: one `##` section per module file or package.

---

## services/market_data.py

- **Graceful Degradation**: Service enters degraded mode after `HFT_RECORD_DEGRADE_THRESHOLD` (default 500) consecutive recorder queue drops. In degraded mode recording is skipped but ticks still flow. Auto-recovers when queue drops below 50%.
- **Renamed**: `_on_shioaji_event` → `_on_broker_event`; `_record_shioaji_crash_signature` → `_record_broker_crash_signature`. Metric: `broker_crash_signature_total` (was `shioaji_crash_signature_total`).
- **Crash Detector Injection**: `crash_detector: Callable` is now an optional `__init__` parameter. Shioaji factory injects `detect_crash_signature`; Fubon can inject its own or leave None.

## feed_adapter/normalizer.py

- **One-sided LOB**: Snapshots can arrive with only bid or only ask side populated (pre-market, illiquid instruments). Always guard for `None`/empty arrays on each side independently.
- **Rust Fast Path**: `normalize_quote` and `compute_stats` have Rust implementations (~5us vs ~500us Python). CI validates both paths. Python fallback must stay in sync with Rust semantics.
- **NormalizerFieldMap**: Frozen dataclass for broker-specific field names. Default values = Shioaji field names (zero regression). `_is_default_map=True` enables Rust fast paths.

## feed_adapter/shioaji_client.py

- **WebSocket Thread**: All Shioaji callbacks run in a broker-owned thread, not the asyncio event loop. Use `loop.call_soon_threadsafe()` to hand off events to the main loop.
- **Quote Version**: `HFT_QUOTE_VERSION=auto` probes the SDK at runtime. Pinning to a specific version avoids probe overhead on repeated logins.
- **Session Refresh**: Background thread calls `sdk.renew_token()` on a timer. Never call this from the event loop — it is a blocking HTTP call.

## feed_adapter/lob_engine.py

- **Pre-allocated Arrays**: LOB arrays are allocated once in `__init__`. The engine mutates them in-place on every update (Allocator Law). Never replace the arrays with new allocations.
- **SoA Layout**: Bids and asks are stored as parallel price/size arrays (Cache Law). Incoming AoS data from brokers must be flattened immediately.

## risk/engine.py

- **FastGate Rust Binding**: `FastGate` from `rust_core` is the hot-path gate. The Python `RiskEngine` class is the orchestrator; only FastGate touches per-tick data.
- **StormGuard FSM**: 3-state FSM (normal → degraded → halted). Transitions are logged via structlog. Never read `storm_guard_state` from multiple threads without the lock.

## strategy/runner.py

- **Circuit Breaker**: 3-state FSM (normal → degraded → halted). Auto-recovers after `HFT_STRATEGY_CIRCUIT_COOLDOWN_S` (default 60s). Degraded requires N/2 consecutive successes to recover.
- **Typed Intent Channel**: `HFT_TYPED_INTENT_CHANNEL=1` makes `_intent_factory()` return a plain tuple (no `OrderIntent` allocation on hot path). Gateway deserializes lazily.

## feed_adapter/broker_registry.py

- **Module-Level Registry**: `_BROKER_REGISTRY` is a module-level dict. Brokers auto-register on import via side effects in their `__init__.py`. Import order matters in bootstrap.
- **Factory Lookup**: `get_broker_factory(name)` raises `ValueError` for unknown broker names. Always catch and provide clear error message.
- **Default Broker**: `HFT_BROKER` env var defaults to `"shioaji"` if unset.
- **GOTCHA**: If a broker package fails to import (missing SDK), registration silently skips. The broker will only fail at `get_broker_factory()` time.

## feed_adapter/fubon/ (Fubon broker package)

- **SDK Import Guard**: All Fubon modules use `try: import fubon_neo ... except ImportError: fubon_neo = None`. SDK is NOT on PyPI — requires platform-specific `.whl` file.
- **WebSocket Thread**: Like Shioaji, WebSocket callbacks run in a broker thread. Must use `loop.call_soon_threadsafe()` for event loop integration.
- **Pre-allocated Buffers**: `FubonMarketDataProvider.__init__` pre-allocates numpy arrays for 5-level bid/ask books (Allocator Law). Reused per message, never re-created.
- **Price Conversion**: Fubon sends price strings (`"523.00"`). Convert via `Decimal(str) * 10000` → int. NEVER use `float()`. Helper: `_scaled_int_to_price_str()` for outgoing orders.
- **Book Format**: Fubon sends `bids[{price, size}]` (array of objects). Must flatten to parallel arrays immediately on receipt (Cache Law: SoA > AoS).
- **Account Object**: `sdk.login()` returns `accounts` with `.data` list. Use `accounts.data[0]` for primary account.
- **Response Unwrapping**: Fubon SDK returns wrapped responses. Use `_unwrap_list()` / `_unwrap_scalar()` helpers to extract data.
- **Rate Limits**: REST API returns HTTP 429 when rate limit exceeded. Use WebSocket for real-time data to avoid limits.
- **GOTCHA**: `fubon-neo` package name uses hyphen but import is `fubon_neo` (underscore). `pyproject.toml` uses `fubon-neo` in dependencies.

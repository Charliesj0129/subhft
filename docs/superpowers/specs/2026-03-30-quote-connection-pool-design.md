# Quote Connection Pool Design Spec

**Date**: 2026-03-30
**Status**: Draft
**Goal**: Expand quote subscription capacity from 200 (single connection) to 1000 (5 connections) by pooling multiple Shioaji sessions.

## Motivation

Shioaji allows 5 simultaneous connections per person ID, each with 200 quote subscription slots. The platform currently uses only 1 connection for market data (78 symbols). To cover full TXO options chains (weekly + monthly), TW100+ stocks, and commodity futures (gold, oil, TSMC ADR, etc.), we need ~290-500 subscriptions — exceeding the single-connection limit.

## Requirements

1. **Scale**: Support ~300-500 symbols across 3-4 quote connections (within the 5-connection cap including order_client).
2. **Static allocation**: Symbols assigned to connections at startup via `group` field in `symbols.yaml`. No runtime migration.
3. **Unified callback**: All connections funnel callbacks into the same `raw_queue` via `call_soon_threadsafe`. Downstream pipeline (MarketDataService, Normalizer, LOBEngine, FeatureEngine, StrategyRunner) unchanged.
4. **Independent lifecycle**: Each connection has its own watchdog, reconnect orchestrator, and session refresh. One connection's failure does not affect others.
5. **Backward compatible**: `HFT_QUOTE_CONNECTIONS=1` (default) preserves current single-connection behavior with zero code path changes.

## Design

### §1: Symbol Allocation Model

#### symbols.yaml `group` field

```yaml
symbols:
  # Group 0: Futures (core trading)
  - code: TXFC0
    exchange: TAIFEX
    product_type: futures
    group: 0

  # Group 1: TXO options
  - code: TXO18000C202604W2
    exchange: TAIFEX
    product_type: options
    group: 1

  # Group 2: Stocks + commodity futures
  - code: "2330"
    exchange: TSE
    product_type: stock
    group: 2
```

#### Allocation rules

- `group` value maps to connection index (0, 1, 2, ...).
- **Omitted `group`**: defaults to `group: 0` (backward compatible — existing symbols.yaml works without changes).
- Connection count = `max(group) + 1`, capped by `HFT_QUOTE_CONNECTIONS` env var (default 1).

#### Suggested group layout

| Group | Purpose | Est. symbols |
|-------|---------|-------------|
| 0 | Futures (TX/MX/TMF/gold/oil/etc.) | ~20 |
| 1 | TXO weekly + monthly options | ~150 |
| 2 | Stocks TW100+ | ~120 |

Total: ~290, 3 quote connections + 1 order_client = 4 connections (within 5 limit).

### §2: QuoteConnectionPool Class

#### Location

New file: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`

#### Structure

```python
class QuoteConnectionPool:
    """Manages multiple ShioajiClient instances for quote subscriptions.

    Each client owns an independent sj.Shioaji() session with its own
    watchdog, reconnect orchestrator, and subscription tracking. All clients
    share the same callback function, funneling data into a single raw_queue.
    """

    __slots__ = ("_clients", "_group_map", "_num_conns", "_symbols_path", "_config")

    def __init__(self, symbols_path: str, shioaji_cfg: dict, num_conns: int):
        # Validate constraints (see §5 fail-fast checks)
        # Split symbols by group
        # Create num_conns ShioajiClient instances, each with its group's symbols
        ...
```

#### Core methods

| Method | Responsibility |
|--------|---------------|
| `login_all()` | Sequentially login each connection (same API key, independent sessions). Interval between logins: `HFT_QUOTE_LOGIN_INTERVAL_S` (default 2s). |
| `subscribe_all(cb)` / `subscribe_basket(cb)` | Each connection calls `subscribe_basket(cb)` for its own symbol subset. `subscribe_basket` is a duck-type alias for `subscribe_all`. |
| `logout_all()` / `logout()` | Logout all connections. |
| `get_client(group: int)` | Return specific connection instance (diagnostics). |
| `health()` | Aggregated health status of all connections. |

#### Symbol splitting at init

```python
for group_id in range(num_conns):
    group_symbols = [s for s in all_symbols if s.get("group", 0) == group_id]
    client = ShioajiClient(symbols=group_symbols, config=shioaji_cfg)
    self._clients.append(client)
```

#### Login serialization

Shioaji login is rate-limited. Multiple connections login sequentially with configurable interval:

```
conn[0].login() → wait HFT_QUOTE_LOGIN_INTERVAL_S → conn[1].login() → wait → conn[2].login()
```

### §3: Bootstrap Integration + MarketDataService Adaptation

#### bootstrap.py changes

`_build_broker_clients` return type becomes `QuoteConnectionPool | ShioajiClientFacade`:

```python
def _build_broker_clients(...) -> tuple[QuoteConnectionPool | ShioajiClientFacade, Any]:
    num_conns = int(os.getenv("HFT_QUOTE_CONNECTIONS", "1"))

    if num_conns <= 1:
        # Backward compatible: single md_client, identical to current behavior
        return ShioajiClientFacade(symbols_path, base_shioaji_cfg), ShioajiClientFacade(symbols_path, order_cfg)
    else:
        pool = QuoteConnectionPool(symbols_path, base_shioaji_cfg, num_conns)
        order_client = ShioajiClientFacade(symbols_path, order_cfg)
        return pool, order_client
```

#### Duck-type compatibility

`QuoteConnectionPool` exposes the same interface as `ShioajiClientFacade` for methods used by `MarketDataService`. This means **MarketDataService requires zero changes**:

| Method/Property | Pool behavior |
|----------------|---------------|
| `subscribe_basket(cb)` | Alias for `subscribe_all(cb)` |
| `login()` / `login_with_retry()` | Sequential login all clients |
| `logout()` | Logout all clients |
| `logged_in` (property) | `True` only if ALL clients logged in |
| `partial_login` (property) | `True` if at least one client logged in |
| `subscribed_count` (property) | Sum of all clients' `subscribed_count` |
| `mode` (property) | Proxy from `_clients[0].mode` |
| `symbols` (property) | Concatenation of all clients' symbol lists |

### §4: Reconnect, Watchdog, and Observability

#### Independent lifecycle

Each `ShioajiClient` retains its full existing machinery:
- `ReconnectOrchestrator` (exponential backoff, independent cooldown timer)
- `QuoteRuntime` watchdog (independent `_last_quote_data_ts`)
- `SessionRuntime` session refresh thread

Pool does not interfere with per-connection reconnect logic.

#### Health aggregation

```python
def health(self) -> dict[int, dict]:
    return {
        i: {
            "logged_in": c.logged_in,
            "subscribed_count": c.subscribed_count,
            "last_quote_ts": c._last_quote_data_ts,
            "reconnect_count": c._reconnect_count,
        }
        for i, c in enumerate(self._clients)
    }
```

#### Prometheus metrics

Add `conn_id` label to distinguish connections:

| Metric | Labels | Description |
|--------|--------|-------------|
| `hft_quote_conn_subscribed_count` | `conn_id` | Subscribed symbol count per connection |
| `hft_quote_conn_logged_in` | `conn_id` | Login state (0/1) |
| `hft_quote_conn_reconnect_total` | `conn_id` | Cumulative reconnect count |
| `hft_quote_conn_last_data_age_s` | `conn_id` | Seconds since last quote data |

No new metrics module — uses existing `prometheus_client` gauges/counters within Pool.

#### structlog context

Each connection's logs tagged with `conn_id`:

```python
logger.bind(conn_id=i).info("Connection logged in", subscribed=count)
```

### §5: Backward Compatibility, Environment Variables, and Error Handling

#### Backward compatibility guarantees

| Scenario | Behavior |
|----------|----------|
| `HFT_QUOTE_CONNECTIONS` unset or `=1` | Original code path, single `ShioajiClientFacade`, no Pool created |
| `symbols.yaml` without `group` field | All symbols fallback to `group: 0`, identical to current behavior |
| `HFT_BROKER=fubon` | Unaffected. Pool is Shioaji-only. Fubon stays single-connection |

#### New environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HFT_QUOTE_CONNECTIONS` | `1` | Number of quote connections (1 = original behavior) |
| `HFT_QUOTE_LOGIN_INTERVAL_S` | `2` | Delay between sequential logins (seconds) |

#### Fail-fast validation at startup

Pool constructor raises `ValueError` on any of:

1. **Connection cap**: `num_conns + 1 (order_client) > 5` → `"Total connections {n} exceeds Shioaji limit of 5"`
2. **Group subscription cap**: any group has > 200 symbols → `"Group {g} has {n} symbols, exceeds 200 limit"`
3. **Group range**: symbol has `group >= num_conns` → `"Symbol {code} has group={g} but only {num_conns} connections configured"`
4. **Empty group**: log `WARNING` (does not block startup, allows reserved empty slots)

#### Partial login handling

```
conn[0].login() ✓
conn[1].login() ✗ (timeout)
conn[2].login() ✓
```

- Failed connection logs `ERROR`; its symbols are not subscribed.
- **Does not block other connections** — futures connection should not stop because options connection failed.
- `logged_in` property returns `False` (not all connected), but `subscribe_all` proceeds for successfully logged-in connections.
- `partial_login` property returns `True` if at least one connection succeeded.

## Files Changed

| File | Change |
|------|--------|
| `feed_adapter/shioaji/quote_connection_pool.py` | **NEW** — QuoteConnectionPool class |
| `services/bootstrap.py` | Branch on `HFT_QUOTE_CONNECTIONS` to create Pool or single Facade |
| `config/base/symbols.yaml` | Add `group` field to symbol entries |
| `tests/unit/test_quote_connection_pool.py` | **NEW** — Pool unit tests |

**Unchanged**: `MarketDataService`, `Normalizer`, `LOBEngine`, `FeatureEngine`, `StrategyRunner`, `RecorderService`, `RiskEngine`, `OrderAdapter`.

## Not In Scope

- Dynamic subscription migration between connections at runtime
- Fubon multi-connection support (Fubon stays single-connection)
- Automatic group assignment by product_type (manual `group` field in YAML)
- Connection count auto-scaling based on symbol count

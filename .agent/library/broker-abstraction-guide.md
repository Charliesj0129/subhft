# Broker Abstraction Guide

> Multi-broker architecture reference for the HFT platform. Covers protocols, registry, factory pattern, and broker-specific differences.

Date: 2026-03-11
Status: Implementation in progress (protocols defined; Fubon adapter scaffolded)
Companion docs:
- `.agent/library/multi-broker-architecture.md` — design specification
- `.agent/library/current-architecture.md` — full platform architecture baseline
- `.agent/library/shioaji-client-resilience-decoupling-plan.md` — Shioaji decomposition plan
- `docs/architecture/current-architecture.md` — canonical architecture doc

---

## Architecture Overview

```
HFT_BROKER env var
  → broker_registry.get_broker_factory(name)
    → BrokerFactory.create_clients(symbols_path, config)
      → ClientFacade (composes sub-runtimes)
        → MarketDataProvider (protocol)
        → OrderExecutor (protocol)
        → AccountProvider (protocol)
        → BrokerSession (protocol)
```

The platform uses structural subtyping (`typing.Protocol`) so broker adapters satisfy interfaces without inheritance. `runtime_checkable` decorators allow `isinstance()` checks at service injection points.

## File Map

| File | Purpose |
|------|---------|
| `feed_adapter/protocols.py` | 4 runtime-checkable protocols |
| `feed_adapter/broker_registry.py` | Registry + factory pattern |
| `feed_adapter/normalizer.py` | `NormalizerFieldMap` for broker-specific fields |
| `feed_adapter/shioaji/` | Shioaji broker package |
| `feed_adapter/fubon/` | Fubon broker package |
| `services/bootstrap.py` | Wires factory into platform startup |
| `services/market_data.py` | Accepts injectable `crash_detector` |

## Protocol Definitions (feed_adapter/protocols.py)

Four `@runtime_checkable` protocols define the broker contract:

### MarketDataProvider

Consumed by `MarketDataService` for market data subscription and snapshots.

```python
def subscribe_basket(self, cb: Callable[..., Any]) -> None: ...
def fetch_snapshots(self) -> list[Any]: ...
def resubscribe(self) -> bool: ...
def reload_symbols(self) -> None: ...
def validate_symbols(self) -> list[str]: ...
```

### OrderExecutor

Consumed by `OrderAdapter` for order lifecycle management.

```python
def place_order(self, contract_code, exchange, action, price, qty, order_type, tif, **kwargs) -> Any: ...
def cancel_order(self, trade: Any) -> Any: ...
def update_order(self, trade: Any, price: float | None, qty: int | None) -> Any: ...
def get_exchange(self, symbol: str) -> str: ...
def set_execution_callbacks(self, on_order, on_deal) -> None: ...
```

Note: The `OrderExecutor` protocol signature uses `price: float` because that is the broker SDK API surface. The platform internally uses scaled integers (x10000). The broker adapter is responsible for converting: callers pass a scaled int, the adapter converts to the broker's expected format via `_scaled_int_to_price_str()` before the SDK call. `float` never crosses the internal accounting boundary.

### AccountProvider

Consumed by `ReconciliationService` and the facade for position/balance queries.

```python
def get_positions(self) -> list[Any]: ...
def get_account_balance(self, account: Any = None) -> Any: ...
def get_margin(self, account: Any = None) -> Any: ...
def list_position_detail(self, account: Any = None) -> list[Any]: ...
def list_profit_loss(self, account, begin_date, end_date) -> list[Any]: ...
```

### BrokerSession

Consumed by `MarketDataService` and `SystemBootstrapper` for session lifecycle.

```python
def login(self, **kwargs) -> Any: ...
def reconnect(self, reason: str = "", force: bool = False) -> bool: ...
def close(self, logout: bool = False) -> None: ...
def shutdown(self, logout: bool = False) -> None: ...
@property
def logged_in(self) -> bool: ...
```

## Protocol Conformance

| Protocol | Shioaji | Fubon | Required |
|----------|---------|-------|----------|
| `MarketDataProvider` | `ShioajiClientFacade` | `FubonMarketDataProvider` (planned) | Yes |
| `OrderExecutor` | `ShioajiClientFacade` | `FubonOrderGateway` (planned) | Yes |
| `AccountProvider` | `ShioajiClientFacade` | `FubonAccountGateway` (planned) | Yes |
| `BrokerSession` | `SessionRuntime` | `FubonSessionRuntime` (planned) | Yes |

## Data Flow Differences

### Market Data

| Aspect | Shioaji | Fubon |
|--------|---------|-------|
| Transport | Callback-driven (`api.quote.subscribe()`) | WebSocket (`sdk.init_realtime()`, `trades`+`books` channels) |
| SDK Init | `sj.Shioaji()` | `FubonSDK()` |
| Tick Format | Dict with `close`, `volume`, `ts` | WebSocket message with trade data |
| Book Format | Parallel arrays (`bid_price[]`, `ask_price[]`) | Array of objects `bids[{price, size}]` → flattened |
| Thread Model | Broker thread → `loop.call_soon_threadsafe()` | Same pattern (WebSocket callback thread) |

### Price Handling

| Aspect | Shioaji | Fubon |
|--------|---------|-------|
| Incoming format | Native numeric (int or float from API) | Price strings (`"523.00"`) |
| Conversion | Multiply by scale (x10000) | `Decimal(str) * 10000` → int |
| Outgoing (orders) | Scaled int directly | `str(Decimal(price) / 10000)` via `_scaled_int_to_price_str()` |
| Rule | Never use `float` | Never use `float`; always `Decimal(str)` |

HFT Constitution law: All price values crossing the broker boundary must be scaled integers (x10000). Broker adapters own conversion in both directions.

### Order Placement

| Aspect | Shioaji | Fubon |
|--------|---------|-------|
| API | `api.place_order(contract, order)` | `sdk.stock.place_order(acc, Order(...))` |
| Contract resolution | `api.Contracts.Stocks[symbol]` | Direct symbol string |
| Enums | `sj.constant.Action.Buy` | `BSAction.Buy` (mapped via `ACTION_MAP`) |

## Bootstrap Sequence

1. `bootstrap.py` reads `HFT_BROKER` env var (default: `"shioaji"`)
2. Imports `feed_adapter.{broker}` package — triggers auto-registration in `__init__.py`
3. `get_broker_factory(broker_name)` retrieves registered factory
4. `factory.create_clients(symbols_path, config)` creates the facade
5. Facade sub-runtimes are wired into platform services
6. Broker-specific crash detector injected into `MarketDataService`
7. Broker-specific `NormalizerFieldMap` applied to normalizer

If `HFT_BROKER` is not registered, `get_broker_factory()` raises immediately (fail-fast).

## Normalizer Field Map

`NormalizerFieldMap` is a frozen dataclass mapping generic field names to broker-specific ones:

- Default values = Shioaji field names (zero regression for existing deployments)
- `_is_default_map=True` enables Rust fast paths (bypasses Python field lookups)
- Fubon provides `FUBON_FIELD_MAP` from `feed_adapter/fubon/normalizer_fields.py`

## Testing Patterns

- **Protocol conformance**: `assert isinstance(facade, MarketDataProvider)`
- **Mock SDK**: Use `unittest.mock.MagicMock()` for broker SDK objects
- **Import guard**: `pytest.importorskip("fubon_neo")` for Fubon-only tests
- **Factory test**: Register mock factory, verify `get_broker_factory()` returns it
- **Isolation**: Each broker's tests must pass without the other's SDK installed

## Adding a New Broker

1. Create `feed_adapter/{broker}/` package
2. Implement all 4 protocols in separate files:
   - `session_runtime.py` — `BrokerSession`
   - `market_data.py` — `MarketDataProvider`
   - `order_gateway.py` — `OrderExecutor`
   - `account_gateway.py` — `AccountProvider`
3. Create `{Broker}ClientFacade` in `facade.py` composing sub-runtimes
4. Create `{Broker}BrokerFactory` in `factory.py` implementing `BrokerFactory`
5. Auto-register in `__init__.py` via `register_broker(name, factory_instance)`
6. Create `normalizer_fields.py` with `{BROKER}_FIELD_MAP`
7. Guard SDK import with `try/except ImportError`
8. Add optional dependency to `pyproject.toml`
9. Write unit tests with mocked SDK
10. Measure and record latency profile in `config/research/latency_profiles.yaml`
11. Update the Protocol Conformance table in this file and reference it from `.agent/rules/25-architecture-governance.md` Rule 9
12. Add config file at `config/base/brokers/{broker}.yaml`

## Invariants (from Architecture Governance Rule 9)

1. Platform Core must never import from `feed_adapter/{broker}/` directly. All access is via protocols.
2. All prices crossing the broker boundary must be scaled integers (x10000). Adapters own conversion.
3. Each broker adapter must have a latency profile before any alpha can be promoted on that broker.
4. `get_broker_factory()` raises immediately on unknown broker — no silent fallback.
5. Execution normalization is data-driven via `BrokerExecFieldMap`, not broker-specific code branches.
6. Broker SDK imports must be guarded with `try/except ImportError` in all adapter files.
7. `__init__.py` auto-registers the factory so bootstrap only needs to import the package.

## Red Flags (HFT Constitution Compliance)

- `float` used for price in adapter conversion → REJECT (use `Decimal(str)` or scaled int)
- Broker SDK imported at module top without `try/except ImportError` → REJECT
- Platform Core (strategy/risk/order) importing from `feed_adapter/shioaji/` directly → REJECT
- Alpha promoted without Fubon latency profile when running on Fubon → REJECT
- `print()` in adapter code instead of `structlog` → REJECT
- `datetime.now()` in adapter instead of `timebase.now_ns()` → REJECT

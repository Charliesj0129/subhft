---
name: broker-abstraction
description: Use when working with the multi-broker abstraction layer — protocols, broker registry, factory pattern, normalizer field maps, or adding a new broker to the platform.
---

# Broker Abstraction Layer

## 1. Protocol Layer (`feed_adapter/protocols.py`)

4 runtime-checkable protocols that every broker must implement:

| Protocol | Key Methods | Purpose |
|----------|------------|---------|
| `MarketDataProvider` | `subscribe_basket(cb)`, `fetch_snapshots()`, `resubscribe()`, `reload_symbols()`, `validate_symbols()` | Market data ingestion |
| `OrderExecutor` | `place_order()`, `cancel_order()`, `update_order()`, `get_exchange()`, `set_execution_callbacks()` | Order routing |
| `AccountProvider` | `get_positions()`, `get_account_balance()`, `list_position_detail()`, `list_profit_loss()` | Account queries |
| `BrokerSession` | `login()`, `reconnect()`, `close()`, `shutdown()`, `logged_in` | Session lifecycle |

All protocols use `@runtime_checkable` for `isinstance` checks.

---

## 2. Broker Registry (`feed_adapter/broker_registry.py`)

```python
class BrokerFactory(Protocol):
    def create_clients(self, symbols_path, broker_config) -> tuple[Any, Any]: ...

# Registry API
register_broker(name: str, factory: BrokerFactory) -> None
get_broker_factory(name: str) -> BrokerFactory  # raises ValueError if unknown
list_brokers() -> list[str]
```

- `_BROKER_REGISTRY`: module-level dict storing registered factories
- Brokers auto-register on import (side effect in `__init__.py`)
- `HFT_BROKER` env var selects active broker (default: `"shioaji"`)

---

## 3. Bootstrap Wiring (`services/bootstrap.py`)

```
_build_broker_clients():
  broker_name = os.getenv("HFT_BROKER", "shioaji")
  factory = get_broker_factory(broker_name)
  client, facade = factory.create_clients(symbols_path, broker_config)
```

The factory handles all broker-specific configuration internally.

---

## 4. Normalizer Field Map (`feed_adapter/normalizer.py`)

`NormalizerFieldMap` is a frozen dataclass mapping generic field names to broker-specific ones:

| Field | Shioaji Default | Purpose |
|-------|----------------|---------|
| `symbol` | `"code"` | Symbol identifier |
| `price` | `"close"` | Last price |
| `volume` | `"volume"` | Trade volume |
| `ts` | `"datetime"` | Timestamp |
| `bid_price` | `"bid_price"` | Bid prices array |
| `ask_price` | `"ask_price"` | Ask prices array |
| `bid_volume` | `"bid_volume"` | Bid volumes array |
| `ask_volume` | `"ask_volume"` | Ask volumes array |
| `simtrade` | `"simtrade"` | Simulated trade flag |
| `odd_lot` | `"intraday_odd"` | Odd lot flag |

- Default values = Shioaji field names (zero regression)
- `_is_default_map=True` enables Rust fast paths (bypasses Python field lookups)
- Each broker provides its own field map (e.g., `FUBON_FIELD_MAP` in `fubon/normalizer_fields.py`)

---

## 5. Crash Detection Injection

`MarketDataService.__init__` accepts optional `crash_detector: Callable[[str | None], str | None]`:
- Shioaji factory injects `detect_crash_signature` from `shioaji/signatures.py`
- Fubon factory can inject its own detector or leave as `None`
- Metric: `broker_crash_signature_total` (renamed from `shioaji_crash_signature_total`)

---

## 6. Registered Brokers

| Broker | Package | Factory | Facade | SDK |
|--------|---------|---------|--------|-----|
| `shioaji` (default) | `feed_adapter/shioaji/` | `ShioajiBrokerFactory` | `ShioajiClientFacade` | `shioaji` (PyPI) |
| `fubon` | `feed_adapter/fubon/` | `FubonBrokerFactory` | `FubonClientFacade` | `fubon-neo` (.whl) |

---

## 7. Adding a New Broker — Checklist

1. Create package: `feed_adapter/<broker>/`
2. Implement `BrokerSession` → `session_runtime.py`
3. Implement `MarketDataProvider` → `market_data.py`
4. Implement `OrderExecutor` → `order_gateway.py`
5. Implement `AccountProvider` → `account_gateway.py`
6. Create `<Broker>ClientFacade` → `facade.py` (compose sub-runtimes)
7. Create `<Broker>BrokerFactory` → `factory.py` (implement `BrokerFactory` protocol)
8. Auto-register in `__init__.py`: `register_broker("<broker>", <Broker>BrokerFactory())`
9. Create `normalizer_fields.py` with broker-specific `NormalizerFieldMap`
10. Guard SDK import: `try: import <sdk> except ImportError: <sdk> = None`
11. Add optional dependency to `pyproject.toml`: `<broker> = ["<sdk>>=x.y.z"]`
12. Write unit tests with mocked SDK
13. Update `.agent/` docs: create skill, update Rule 9, update broker abstraction guide

---

## 8. Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_BROKER` | `shioaji` | Select active broker: `shioaji` or `fubon` |

---

## 9. Common Testing Patterns

```python
# Protocol conformance
assert isinstance(facade, MarketDataProvider)
assert isinstance(facade, OrderExecutor)
assert isinstance(facade, AccountProvider)
assert isinstance(facade.session, BrokerSession)

# Mock SDK
from unittest.mock import MagicMock
mock_sdk = MagicMock()

# Import guard in tests
fubon_neo = pytest.importorskip("fubon_neo")

# Broker selection via env
monkeypatch.setenv("HFT_BROKER", "fubon")
```

---

## Related Skills

- `shioaji-contracts` — Shioaji broker details
- `fubon-contracts` — Fubon broker details

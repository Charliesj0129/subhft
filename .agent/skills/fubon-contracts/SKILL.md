---
name: fubon-contracts
description: Use when working with Fubon (富邦) broker integration — session lifecycle, market data WebSocket, order placement, account queries via the fubon-neo SDK.
---

# Fubon Contracts & Session Management

## Architecture Overview

`FubonClientFacade` composes 4 sub-runtime modules under `feed_adapter/fubon/`:

| Submodule | File | Responsibility |
|-----------|------|----------------|
| `FubonSessionRuntime` | `feed_adapter/fubon/session_runtime.py` | Login, reconnect with backoff, shutdown |
| `FubonMarketDataProvider` | `feed_adapter/fubon/market_data.py` | WebSocket trades+books subscription |
| `FubonOrderGateway` | `feed_adapter/fubon/order_gateway.py` | Order place/cancel/modify |
| `FubonAccountGateway` | `feed_adapter/fubon/account_gateway.py` | Positions, balance, P&L |
| `FubonClientFacade` | `feed_adapter/fubon/facade.py` | Composes all sub-runtimes |
| `FubonBrokerFactory` | `feed_adapter/fubon/factory.py` | `BrokerFactory` protocol impl |
| `FubonClientConfig` | `feed_adapter/fubon/_config.py` | Frozen dataclass config from env vars |
| Constants | `feed_adapter/fubon/_constants.py` | Enum mapping dicts |
| `FUBON_FIELD_MAP` | `feed_adapter/fubon/normalizer_fields.py` | Normalizer field name mapping |

---

## FubonSessionRuntime

### Login Flow

```python
from fubon_neo.sdk import FubonSDK

sdk = FubonSDK()
accounts = sdk.login(id, password, cert_path, cert_password)
account = accounts.data[0]  # Primary account

sdk.init_realtime()  # Initialize WebSocket connections
```

### Key Methods

| Method | Purpose |
|--------|---------|
| `login()` | `FubonSDK().login(id, pw, cert, cert_pw)` → stores `accounts.data[0]` |
| `reconnect()` | Exponential backoff reconnection |
| `close()` | Close active connections |
| `shutdown()` | Full shutdown of SDK resources |
| `logged_in` | Property: whether session is active |

### Reconnect Policy

- Certificate errors: raise immediately (wrong path or password).
- Network errors: retry with exponential backoff.
- Cert password defaults to `FUBON_ID` if `FUBON_CERT_PASSWORD` is unset.

---

## FubonMarketDataProvider

Implements `MarketDataProvider` protocol.

### Subscription

```python
# subscribe_basket(cb) internally:
sdk.init_realtime()
# Subscribes to 'trades' and 'books' WebSocket channels
# Callbacks run in a broker thread — use loop.call_soon_threadsafe()
```

### Book Data Handling

Fubon sends books as array of objects:
```python
# Incoming: bids = [{"price": "523.00", "size": 10}, ...]
# Flattened to pre-allocated numpy arrays (Allocator Law):
# bid_prices[0..4], bid_volumes[0..4] (5-level depth)
```

### Price Conversion (Precision Law)

```python
from decimal import Decimal

# Incoming price string → scaled int
price_scaled = int(Decimal(price_str) * 10000)

# NEVER: price_scaled = int(float(price_str) * 10000)  ← float error
```

---

## FubonOrderGateway

Implements `OrderExecutor` protocol.

### Order Placement

```python
from fubon_neo.sdk import Order

order = Order(
    buy_sell=BSAction.Buy,
    symbol='2330',
    price='523.00',        # String price for Fubon SDK
    quantity=1,
    price_type=PriceType.Limit,
    time_in_force=TimeInForce.ROD,
    order_type=OrderType.Stock,
)

result = sdk.stock.place_order(account, order)
```

### Price Conversion (Outgoing)

```python
# Platform scaled int → Fubon price string
def _scaled_int_to_price_str(price: int, scale: int = 10000) -> str:
    return str(Decimal(price) / Decimal(scale))
```

### Constant Maps

| Map | Purpose |
|-----|---------|
| `ACTION_MAP` | Platform action → Fubon `BSAction` |
| `TIF_MAP` | Platform TIF → Fubon `TimeInForce` |
| `ORDER_TYPE_MAP` | Platform type → Fubon `OrderType` |
| `PRICE_TYPE_MAP` | Platform price type → Fubon `PriceType` |
| `ORDER_STATUS_MAP` | Fubon status → Platform status |

### Order Operations

| Operation | Method |
|-----------|--------|
| Place | `sdk.stock.place_order(account, order)` |
| Cancel | `sdk.stock.cancel_order(account, order_result)` |
| Modify price | `sdk.stock.modify_price(account, order_result, new_price)` |
| Modify qty | `sdk.stock.modify_quantity(account, order_result, new_qty)` |

---

## FubonAccountGateway

Implements `AccountProvider` protocol.

### Key Methods

| Method | SDK Call | Returns |
|--------|---------|---------|
| `get_positions()` | `sdk.accounting.inventories(acc)` | Generic position dicts |
| `get_account_balance()` | `sdk.accounting.query_settlement(acc, "0d")` | Balance info |
| `list_position_detail()` | `sdk.accounting.unrealized_gains_and_loses(acc)` | Position details |
| `list_profit_loss()` | `sdk.accounting.unrealized_gains_and_loses(acc)` | P&L breakdown |

### Response Unwrapping

Fubon SDK returns wrapped responses. Use helpers:
- `_unwrap_list(response)` — extract list data from response wrapper
- `_unwrap_scalar(response)` — extract single value from response wrapper

---

## Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `FUBON_ID` | — | Fubon account ID |
| `FUBON_PASSWORD` | — | Fubon account password |
| `FUBON_CERT_PATH` | — | Path to Fubon certificate file |
| `FUBON_CERT_PASSWORD` | — | Certificate password (defaults to `FUBON_ID`) |
| `FUBON_SIMULATION` | `0` | `1` = use simulation mode |
| `HFT_BROKER` | `shioaji` | Set to `fubon` to activate Fubon broker |

---

## Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: fubon_neo` | SDK not installed | Install platform-specific `.whl` from Fubon |
| Login fails with cert error | Wrong cert path or password | Verify `FUBON_CERT_PATH`; password defaults to `FUBON_ID` |
| HTTP 429 on REST API | Rate limit exceeded | Back off; use WebSocket for real-time data |
| WebSocket disconnects | Network disruption | Auto-reconnect via `session_runtime`; check logs |
| Price mismatch | `float()` used instead of `Decimal` | Always use `Decimal(str)` conversion |
| `fubon_neo` vs `fubon-neo` | Package name uses hyphen, import uses underscore | `import fubon_neo` (underscore) |

---

## Multi-Broker Context

- Selected via `HFT_BROKER=fubon`
- `FubonBrokerFactory` auto-registers on package import (`feed_adapter/fubon/__init__.py`)
- Implements all 4 protocols from `feed_adapter/protocols.py`
- SDK: `fubon-neo` v2.2.8, Python 3.8–3.13, platform-specific wheels (NOT on PyPI)
- SDK import guarded: `try: import fubon_neo except ImportError: fubon_neo = None`

---

## Related Skills

- `shioaji-contracts` — Shioaji broker equivalent
- `broker-abstraction` — Multi-broker protocol and registry layer

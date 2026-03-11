---
name: fubon-tradeapi
description: Use when integrating Fubon (富邦) TradeAPI as a broker, implementing the Fubon feed adapter, diagnosing Fubon connection/order issues, or comparing Fubon vs Shioaji capabilities.
---

# Fubon TradeAPI Reference

## Overview

Fubon TradeAPI is an HTTP/WebSocket-based broker API for TWSE/OTC stocks and futures/options, provided by 富邦證券. Unlike Shioaji's proprietary SDK, Fubon uses standard REST + WebSocket protocols.

- **SDK Version**: v2.2.8
- **Supported Languages**: Python, C++, C#, Node.js, Go
- **Markets**: Taiwan Stock Exchange (TWSE), OTC Securities Center, Taiwan Futures Exchange (TAIFEX)
- **Data History**: Individual stocks back to 2010, indices back to 2015

---

## Authentication

API Key + Password authentication. No certificate file required (unlike Shioaji which needs a CA cert).

```python
from fubon_neo.sdk import FubonSDK

sdk = FubonSDK()
accounts = sdk.login(
    api_key=os.environ["HFT_FUBON_API_KEY"],
    password=os.environ["HFT_FUBON_PASSWORD"],
)
# accounts contains list of available trading accounts
```

### Prerequisites

- Electronic trading certificate application required (broker-side)
- API usage agreement signature mandatory
- Connection testing before production deployment

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_FUBON_API_KEY` | — | Fubon API key (required) |
| `HFT_FUBON_PASSWORD` | — | Fubon account password (required) |
| `HFT_FUBON_WS_URL` | (SDK default) | WebSocket endpoint override |
| `HFT_FUBON_REST_URL` | (SDK default) | REST API endpoint override |

---

## Market Data

### WebSocket Streaming (Real-Time)

Five channel types available:

| Channel | Content | Use Case |
|---------|---------|----------|
| `aggregates` | Composite market data | General quote monitoring |
| `books` | Top-5 bid/ask levels (L2 depth) | LOB state, spread/imbalance |
| `candles` | Minute-level K-lines | Intraday OHLCV |
| `indices` | Index values | Market regime detection |
| `trades` | Individual executions | Tick-level flow, OFI signals |

- Supports multiple simultaneous WebSocket connections (up to 5)
- Ping/pong keepalive: 30-second default interval
- Auto-disconnect after 2 consecutive missed pongs

### REST API (Historical / Snapshot)

**Intraday**: Candles, real-time quotes, ticker lists, trade details, price-volume distribution.

**Historical**: Candles up to 1 year per request. Individual stocks back to 2010, indices back to 2015. 52-week high/low statistics.

**Snapshots**: Volume/value rankings, price movement rankings, market segment quotes.

**Built-in Technical Indicators**: BBANDS, KDJ, MACD, RSI, SMA (server-side computation).

**Futures/Options**: Dedicated endpoints with margin requirement queries and symbol conversion utilities.

---

## Order Execution

### REST API Orders

| Operation | Description |
|-----------|-------------|
| Place order | Single order submission |
| Batch place | Multiple orders in one request |
| Modify order | Change price and/or quantity |
| Batch modify | Bulk modification |
| Cancel order | Single cancellation |
| Batch cancel | Bulk cancellation |
| Order history | Query submitted orders |
| Trade history | Query executed fills |

### Order Parameters

- **Side**: `"B"` (buy) / `"S"` (sell)
- **Price type**: `"L"` (limit) / `"M"` (market)
- **Non-blocking mode**: Asynchronous submission with callback-based reporting

### Smart / Conditional Orders

| Type | Description |
|------|-------------|
| Conditional | Single/multiple condition triggers |
| Day-trading | Intraday-specific condition orders |
| Time-slice | Distribute order over time intervals |
| TPSL | Stop-loss / take-profit |
| Trailing profit | Dynamic trailing stop |

### Account Queries

- Inventory / position queries
- Margin quota (day-trading and standard)
- Maintenance rate monitoring
- Settlement payment queries
- Realized / unrealized P&L
- Bank balance queries

---

## SDK Usage Patterns

### Blocking Mode

```python
from fubon_neo.sdk import FubonSDK

sdk = FubonSDK()
accounts = sdk.login(api_key="...", password="...")
account = accounts[0]

# Place a limit buy order
result = sdk.place_order(
    account=account,
    symbol="2330",
    side="B",
    price_type="L",
    price=580.0,    # NOTE: must scale to int x10000 at platform boundary
    quantity=1,
)
```

### Non-Blocking Mode (Callbacks)

```python
# Async order submission with callback
sdk.set_order_callback(on_order_update)
sdk.place_order_async(account=account, symbol="2330", side="B", ...)

def on_order_update(report):
    # Process order/fill report
    pass
```

### WebSocket Subscription

```python
# Subscribe to real-time book data
ws = sdk.create_ws_connection()
ws.subscribe("books", symbols=["2330", "2317"])
ws.on_message = on_book_update

def on_book_update(msg):
    # msg contains top-5 bid/ask levels
    pass
```

---

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Login fails | Invalid API key or password | Verify `HFT_FUBON_API_KEY` / `HFT_FUBON_PASSWORD` |
| Token expired | Session timeout | Re-login; implement auto-refresh |
| WebSocket disconnect | Network issue or missed pongs | SDK auto-reconnect; tune ping interval |
| Rate limited | Too many requests | Back off; batch operations where possible |
| Order rejected outside hours | Market closed | Check market hours (TWSE 09:00-13:30 TST) |
| Empty book data | Symbol not subscribed or delisted | Verify symbol list; check subscription status |
| Lightweight risk control error | Feature not enabled | Apply through account manager at broker |

---

## HFT Platform Integration Rules

### Precision Law Compliance

All prices from Fubon API arrive as `float`. Convert to scaled `int` (x10000) at the ingestion boundary:

```python
# At FubonNormalizer boundary — NEVER store float prices
price_scaled: int = int(round(raw_price * 10000))
```

### Allocator Law Compliance

Pre-allocate buffers for book data (top-5 = 10 levels):

```python
# Pre-allocate in __init__, reuse in hot path
self._bid_buf = np.zeros((5, 2), dtype=np.int64)  # (price_x10000, qty)
self._ask_buf = np.zeros((5, 2), dtype=np.int64)
```

### Async Law Compliance

Use async HTTP client for REST orders; never block the event loop:

```python
# GOOD: async order submission
async def place_order(self, intent: OrderIntent) -> OrderCommand:
    async with self._session.post(url, json=payload) as resp:
        return parse_response(await resp.json())

# BAD: blocking call on hot path
# result = requests.post(url, json=payload)  # NEVER
```

### Boundary Law Compliance

WebSocket messages should be parsed with `orjson` (zero-copy where possible). Avoid constructing intermediate Python dicts on the hot path.

---

## Fubon vs Shioaji Comparison

| Aspect | Fubon TradeAPI | Shioaji |
|--------|---------------|---------|
| Protocol | REST + WebSocket | Proprietary SDK |
| Auth | API Key + Password | API Key + Secret + CA cert |
| L2 Depth | Top-5 levels | Top-5 levels |
| Historical data | Back to 2010 | Limited |
| Smart orders | TPSL, trailing, time-slice | Basic conditional |
| Session mgmt | Token-based | SDK-managed session |
| Reconnect | SDK auto-reconnect | Manual via `SessionRuntime` |
| Rate limits | Documented thresholds | Undocumented |

---

## Cross-References

| Related Skill | When to Use |
|---------------|-------------|
| `shioaji-contracts` | Shioaji-equivalent session/contract management |
| `multi-broker-ops` | Switching between Fubon and Shioaji, failover procedures |
| `hft-strategy-dev` | Wiring Fubon adapter into strategy runner |
| `hft-architect` | Reviewing multi-broker architecture changes |

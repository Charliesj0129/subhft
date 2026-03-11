# Fubon Neo API Quick Reference

> SDK reference for the `fubon-neo` Python package (v2.2.8). Used by `feed_adapter/fubon/` modules.

## Installation

Platform-specific `.whl` files (NOT on PyPI):
- Windows: `fubon_neo-2.2.8-cp37-abi3-win_amd64.whl`
- macOS ARM: `fubon_neo-2.2.8-cp37-abi3-macosx_11_0_arm64.whl`
- Linux: `fubon_neo-2.2.8-cp37-abi3-manylinux_2_28_x86_64.whl`

Python 3.8–3.13 supported (v2.0.1+).

## Core Classes

### FubonSDK

```python
from fubon_neo.sdk import FubonSDK, Order, FugleAPIError

sdk = FubonSDK()
accounts = sdk.login("ID", "password", "cert_path", "cert_password")
account = accounts.data[0]

# Initialize real-time WebSocket connections
sdk.init_realtime()
```

### Login Methods

| Method | Since | Notes |
|--------|-------|-------|
| ID + Password + Cert | v1.0 | Primary method; cert password defaults to ID |
| API Key | v2.2.7 | Granular permissions, IP whitelisting, max 30 keys |
| Web credential export | v2.2.8 | Export from web interface |

## Market Data

### REST Client

```python
reststock = sdk.marketdata.rest_client.stock

# Intraday
reststock.intraday.quote(symbol='2330')
reststock.intraday.candles(symbol='2330', timeframe='1')  # 1,5,10,15,30,60,D,W,M
reststock.intraday.trades(symbol='2330')
reststock.intraday.volumes(symbol='2330')
reststock.intraday.ticker(symbol='2330')
reststock.intraday.tickers(type='EQUITY', exchange='TWSE', isNormal=True)

# Historical ('from' is a Python keyword — use dict unpacking)
reststock.historical.candles(symbol='0050', **{'from': '2023-02-06', 'to': '2023-02-08'})
reststock.historical.stats(symbol='0050')

# Snapshots
reststock.snapshot.quotes(market='TSE')
reststock.snapshot.movers(market='TSE', direction='up', change='percent')
reststock.snapshot.actives(market='TSE', trade='value')

# Technical indicators (v2.2.6+)
reststock.technical.bb(symbol='2330', timeframe='D', period=20, **date_range)
reststock.technical.rsi(symbol='2330', timeframe='D', period=14, **date_range)
reststock.technical.macd(symbol='2330', timeframe='D', **date_range)
reststock.technical.kdj(symbol='2330', timeframe='D', **date_range)
```

### WebSocket (Real-time)

```python
sdk.init_realtime()
# Subscribe to 'trades' and 'books' channels
# Messages arrive as callbacks with trade/book data
# Book format: bids[{price, size}], asks[{price, size}]
```

### Market Codes

| Code | Market |
|------|--------|
| `TSE` | 上市 (TWSE) |
| `OTC` | 上櫃 (TPEx) |
| `ESB` | 興櫃 |
| `TIB` | 創新板 |

## Order Placement

```python
from fubon_neo.sdk import Order

order = Order(
    buy_sell=BSAction.Buy,
    symbol='2330',
    price='523.00',             # String price
    quantity=1,
    price_type=PriceType.Limit,
    time_in_force=TimeInForce.ROD,
    order_type=OrderType.Stock,
    user_def='tag123'           # Max 10 chars, ASCII 33-126 (v2.2.8)
)

result = sdk.stock.place_order(account, order)
```

### Order Operations

| Operation | Method |
|-----------|--------|
| Place | `sdk.stock.place_order(account, order)` |
| Cancel | `sdk.stock.cancel_order(account, order_result)` |
| Modify price | `sdk.stock.modify_price(account, order_result, new_price)` |
| Modify qty | `sdk.stock.modify_quantity(account, order_result, new_qty)` |
| Query orders | `sdk.stock.get_order_results(account)` |

## Account Queries

```python
sdk.accounting.inventories(account)                    # Positions
sdk.accounting.query_settlement(account, "0d")         # Settlement
sdk.accounting.unrealized_gains_and_loses(account)     # P&L
```

## Error Handling (v2.2.4+)

```python
from fubon_neo.sdk import FugleAPIError

try:
    response = reststock.intraday.quote(symbol='2330')
except FugleAPIError as e:
    print(f"Status: {e.status_code}, Body: {e.response_text}")
```

Rate limit: HTTP 429 → `{"statusCode": 429, "message": "Rate limit exceeded"}`

## Version History

| Version | Key Changes |
|---------|------------|
| v2.2.8 | Web credential export, capital changes API, user_def rules |
| v2.2.7 | API Key login |
| v2.2.6 | Technical indicators, Golang SDK |
| v2.2.4 | Exception-based errors (`FugleAPIError`), C++ SDK |

## HFT Platform Integration

- Package: `feed_adapter/fubon/` (session, market data, orders, account)
- Selected by: `HFT_BROKER=fubon`
- Import guard: `try: import fubon_neo except ImportError: fubon_neo = None`
- Prices: string → `Decimal(str) * 10000` → scaled int (Precision Law)

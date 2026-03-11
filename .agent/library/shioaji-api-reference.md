# Shioaji API Quick Reference

Date: 2026-03-11
Status: Active reference
Scope: SDK reference for the `shioaji` Python package (永豐金證券). Used by `feed_adapter/shioaji/` modules.
Companion docs: `.agent/library/current-architecture.md`, `.agent/library/multi-broker-architecture.md`, `.agent/library/shioaji-client-resilience-decoupling-plan.md`.
Companion skill: `.agent/skills/shioaji-contracts/SKILL.md`.

## 1. Installation

```bash
pip install shioaji
```

Supports Windows, Linux, macOS. Python 3.8+.

## 2. Core Classes

### Shioaji API

```python
import shioaji as sj

api = sj.Shioaji()

# Login
api.login(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    contracts_cb=lambda security_type: print(f"Loaded: {security_type}"),
    subscribe_trade=True,
    fetch_contract=True
)

# Simulation mode
api.login(
    api_key="YOUR_API_KEY",
    secret_key="YOUR_SECRET_KEY",
    contracts_cb=lambda security_type: print(f"Loaded: {security_type}"),
    simulation=True  # Use simulation environment
)
```

## 3. Contracts

```python
# Stock contracts
contract = api.Contracts.Stocks["2330"]        # TSMC
contract = api.Contracts.Stocks.TSE["2330"]

# Futures contracts
contract = api.Contracts.Futures.TXF["TXF202603"]  # TX futures

# All stocks
all_stocks = [c for c in api.Contracts.Stocks]

# Contract attributes
contract.code        # "2330"
contract.symbol      # "TSE2330"
contract.name        # "台積電"
contract.exchange    # Exchange.TSE
contract.category    # "01"
contract.limit_up    # Upper price limit
contract.limit_down  # Lower price limit
contract.reference   # Reference price
```

## 4. Market Data

### Quote Subscription (Callback-driven)

```python
from shioaji import constant as sc

# Subscribe tick data
api.quote.subscribe(
    contract,
    quote_type=sc.QuoteType.Tick,
    version=sc.QuoteVersion.v1
)

# Subscribe bid/ask
api.quote.subscribe(
    contract,
    quote_type=sc.QuoteType.BidAsk,
    version=sc.QuoteVersion.v1
)

# Set callbacks
api.quote.set_on_tick_fop_v1_callback(on_tick_callback)
api.quote.set_on_bidask_fop_v1_callback(on_bidask_callback)

# Callback signature
def on_tick_callback(exchange, tick):
    # tick.close, tick.volume, tick.datetime, tick.simtrade
    pass

def on_bidask_callback(exchange, bidask):
    # bidask.bid_price, bidask.ask_price (arrays)
    # bidask.bid_volume, bidask.ask_volume (arrays)
    pass
```

### Snapshots

```python
snapshots = api.snapshots([contract1, contract2])
# Returns list of snapshot dicts with OHLCV data
```

### Rankings/Scanners

```python
# Volume leaders, price changes, etc.
scanners = api.scanners(scanner_type=sc.ScannerType.VolumeRank)
```

## 5. Order Placement

```python
from shioaji import constant as sc  # same import as Section 4

# Stock order
order = api.Order(
    price=523,                           # Price (numeric)
    quantity=1,
    action=sc.Action.Buy,                # Buy/Sell
    price_type=sc.StockPriceType.LMT,    # LMT/MKT/MKP
    order_type=sc.OrderType.ROD,         # ROD/IOC/FOK
    order_lot=sc.StockOrderLot.Common,   # Common/Odd/IntradayOdd
    account=api.stock_account
)

trade = api.place_order(contract, order)

# Futures order
order = api.Order(
    price=20000,
    quantity=1,
    action=sc.Action.Buy,
    price_type=sc.FuturesPriceType.LMT,
    order_type=sc.OrderType.ROD,
    octype=sc.FuturesOCType.Auto,
    account=api.futopt_account
)

trade = api.place_order(contract, order)
```

### Order Operations

| Operation | Method |
|-----------|--------|
| Place | `api.place_order(contract, order)` |
| Cancel | `api.cancel_order(trade)` |
| Update price | `api.update_order(trade, price=new_price)` |
| Update qty | `api.update_order(trade, quantity=new_qty)` |
| List trades | `api.list_trades()` |
| Update status | `api.update_status(api.stock_account)` |

### Execution Callbacks

```python
api.set_order_callback(order_callback)

def order_callback(stat, msg):
    # stat: OrderState (order status)
    # msg: dict with order details
    pass
```

## 6. Account Queries

```python
# Positions
positions = api.list_positions(api.stock_account)

# Account balance
balance = api.account_balance()

# Margin
margin = api.margin(api.futopt_account)

# P&L
pnl = api.list_profit_loss(api.stock_account)

# Settlement
settlements = api.settlements(api.stock_account)
```

## 7. Key Enums (`shioaji.constant`)

| Enum | Values |
|------|--------|
| `Action` | `Buy`, `Sell` |
| `StockPriceType` | `LMT`, `MKT`, `MKP` |
| `FuturesPriceType` | `LMT`, `MKT`, `MKP` |
| `OrderType` | `ROD`, `IOC`, `FOK` |
| `QuoteType` | `Tick`, `BidAsk` |
| `QuoteVersion` | `v1` |
| `Exchange` | `TSE`, `OTC`, `OES` |
| `StockOrderLot` | `Common`, `Odd`, `IntradayOdd` |

## 8. Key Environment Variables (HFT Platform)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_QUOTE_VERSION` | `auto` | Lock quote protocol: `auto` or `v1` |
| `HFT_SHIOAJI_SKIP_CERT` | `0` | `1` = skip CA cert (non-prod) |
| `HFT_MODE` | `sim` | `sim` uses simulation API |
| `HFT_ORDER_MODE` | — | Order routing mode |
| `HFT_ORDER_SIMULATION` | — | Force simulation orders |
| `HFT_ORDER_NO_CA` | — | Skip CA for orders |

## 9. Common Failure Modes

For deep diagnosis of reconnect, session refresh, and watchdog failures see `.agent/skills/shioaji-contracts/SKILL.md`.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CredentialError` | Wrong API key/secret | Check `.env` credentials |
| `CertificateError` | CA cert not found | `HFT_SHIOAJI_SKIP_CERT=1` or provide cert |
| `QuoteSchemaError` | Schema mismatch | `HFT_QUOTE_VERSION=auto` |
| Stale quotes | Network disruption | Auto-reconnect; check watchdog logs |
| Empty contracts | API timeout | Re-run `make sync-symbols` |
| `patch.object` fails on `SessionRuntime` | `__slots__` prevents instance dict | Use `patch.object(SessionRuntime, 'method')` not instance patching |

## 10. HFT Platform Integration

In the HFT platform, Shioaji is accessed via:

- `feed_adapter/shioaji/` package (session, quote, contracts, orders, account)
- Default broker: `HFT_BROKER=shioaji` (or unset)
- `ShioajiBrokerFactory` auto-registers on import
- Prices: native numeric converted to scaled int (`price_int = price_native * 10000`)

### Price Scaling Convention

Raw Shioaji prices are numeric (can be float). The platform converts immediately at the boundary:

```python
# CORRECT — convert at ingestion boundary
price_scaled: int = int(round(tick.close * 10_000))

# WRONG — never store or pass raw float prices inside the platform
price: float = tick.close  # REJECT per Precision Law
```

### Runtime Plane Classification

| Concern | Runtime Plane | Key Module |
|---------|--------------|------------|
| Session login/refresh | Market Data | `feed_adapter/shioaji/session_runtime.py` |
| Quote callbacks/watchdog | Market Data | `feed_adapter/shioaji/quote_runtime.py` |
| Contract resolution | Market Data | `feed_adapter/shioaji/contracts_runtime.py` |
| Order placement/cancellation | Execution | `order/adapter.py` |
| Fill routing | Execution | `execution/router.py` |

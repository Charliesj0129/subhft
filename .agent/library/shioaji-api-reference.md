# Shioaji API Reference

Date: 2026-03-12
Status: Active reference
Scope: Complete SDK reference for the `shioaji` Python package (永豐金證券 API). Used by `feed_adapter/shioaji/` modules.
Companion docs: `.agent/library/current-architecture.md`, `.agent/library/multi-broker-architecture.md`, `.agent/library/shioaji-client-resilience-decoupling-plan.md`.
Companion skill: `.agent/skills/shioaji-contracts/SKILL.md`.

---

## 1. Installation & Setup

```bash
pip install shioaji
```

- Supported platforms: Windows, Linux, macOS
- Python version: 3.8+
- Dependencies installed automatically: `requests`, `websocket-client`, `pydantic`, etc.

```python
import shioaji as sj

# Production mode
api = sj.Shioaji()

# Simulation mode — connects to Shioaji paper trading environment
api = sj.Shioaji(simulation=True)
```

---

## 2. Authentication

### login()

```python
api.login(
    api_key="YOUR_API_KEY",           # Required — API key from Shioaji console
    secret_key="YOUR_SECRET_KEY",     # Required — secret key from Shioaji console
    person_id="A123456789",           # Optional — ID number for CA certificate auth
    passwd="password",                # Optional — password for CA certificate auth
    fetch_contract=True,              # Default True — auto-fetch all contracts on login
    contracts_timeout=0,              # Default 0 — timeout(s) for contract fetch; 0 = blocking
    contracts_cb=lambda st: None,     # Optional — callback(security_type) fired per contract category loaded
    subscribe_trade=True,             # Default True — auto-subscribe to order/fill callbacks
    receive_window=30000,             # Default 30000 — API request receive window (ms)
)
```

### logout()

```python
api.logout()
```

### Simulation Mode

Pass `simulation=True` at construction time. Login credentials are still required but connect to the paper trading server.

```python
api = sj.Shioaji(simulation=True)
api.login(api_key="KEY", secret_key="SECRET")
```

---

## 3. Account Management

```python
# List all accounts (stock + futures/options)
accounts = api.list_accounts()

# Set default account for orders
api.set_default_account(accounts[0])

# Quick access to default accounts
stock_acct   = api.stock_account      # Default stock account
futopt_acct  = api.futopt_account     # Default futures/options account
```

Account objects expose: `account_id`, `broker_id`, `account_type`, `person_id`.

---

## 4. Contracts

### Access Patterns

```python
# --- Stocks ---
tsmc = api.Contracts.Stocks["2330"]          # By code (searches all exchanges)
tsmc = api.Contracts.Stocks.TSE["2330"]      # TSE (上市)
otc  = api.Contracts.Stocks.OTC["6488"]      # OTC (上櫃)
oes  = api.Contracts.Stocks.OES["00679B"]    # Emerging (興櫃)

# --- Futures ---
txf = api.Contracts.Futures.TXF["TXF202603"]       # TX futures by delivery month
mxf = api.Contracts.Futures.MXF["MXF202603"]       # Mini-TX futures

# --- Options ---
txo = api.Contracts.Options.TXO["TXO202603C20000"]  # Call, strike 20000, 202603 delivery

# --- Indices ---
taiex = api.Contracts.Indexs.TSE["001"]             # TAIEX index (note: "Indexs" not "Indices")

# Iterate all stocks
all_stocks = [c for c in api.Contracts.Stocks]

# Iterate all futures
all_futures = [c for c in api.Contracts.Futures]
```

### Contract Properties

| Property | Type | Description |
|----------|------|-------------|
| `code` | `str` | Symbol code, e.g. `"2330"`, `"TXF202603"` |
| `symbol` | `str` | Exchange-prefixed symbol, e.g. `"TSE2330"` |
| `name` | `str` | Chinese name, e.g. `"台積電"` |
| `exchange` | `Exchange` | `TSE`, `OTC`, `OES`, `TAIFEX` |
| `category` | `str` | Industry category code |
| `delivery_month` | `str` | Futures/options delivery month, e.g. `"202603"` |
| `strike_price` | `float` | Options strike price |
| `option_right` | `str` | `"C"` (call) or `"P"` (put) |
| `limit_up` | `float` | Upper price limit |
| `limit_down` | `float` | Lower price limit |
| `reference` | `float` | Reference (previous close) price |
| `update_date` | `str` | Contract info update date |
| `margin_trading_balance` | `int` | Available margin trading shares (stocks) |
| `short_selling_balance` | `int` | Available short selling shares (stocks) |
| `day_trade` | `str` | Day trade eligibility |

---

## 5. Order Placement & Management

### Constants

```python
from shioaji import constant as sc
```

| Enum | Values | Description |
|------|--------|-------------|
| `Action` | `Buy`, `Sell` | Order side |
| `StockPriceType` | `LMT`, `MKT`, `MKP` | Stock price type (limit, market, market-price) |
| `FuturesPriceType` | `LMT`, `MKT`, `MKP` | Futures price type |
| `OrderType` | `ROD`, `IOC`, `FOK` | Time-in-force |
| `StockOrderLot` | `Common`, `Odd`, `IntradayOdd` | Stock lot type (整股/零股/盤中零股) |
| `StockOrderCond` | `Cash`, `MarginTrading`, `ShortSelling` | Stock order condition |
| `FuturesOCType` | `Auto`, `New`, `Cover`, `DayTrade` | Futures open/close type |

### Stock Order

```python
order = api.Order(
    price=523,
    quantity=1,
    action=sc.Action.Buy,
    price_type=sc.StockPriceType.LMT,
    order_type=sc.OrderType.ROD,
    order_lot=sc.StockOrderLot.Common,
    order_cond=sc.StockOrderCond.Cash,
    account=api.stock_account,
)

trade = api.place_order(
    contract=api.Contracts.Stocks["2330"],
    order=order,
    timeout=0,    # 0 = non-blocking (fire-and-forget); >0 = wait for ack (seconds)
    cb=None,      # Optional callback for async result
)
```

### Futures Order

```python
order = api.Order(
    price=20000,
    quantity=1,
    action=sc.Action.Buy,
    price_type=sc.FuturesPriceType.LMT,
    order_type=sc.OrderType.ROD,
    octype=sc.FuturesOCType.Auto,
    account=api.futopt_account,
)

trade = api.place_order(contract, order)
```

### Non-Blocking Mode

```python
# timeout=0 returns immediately without waiting for exchange ack
trade = api.place_order(contract, order, timeout=0)
```

### Order Modification

```python
# Update price
api.update_order(trade, price=525)

# Update quantity (can only reduce)
api.update_order(trade, qty=2)

# Convenience wrappers
api.update_price(trade, price=525)
api.update_qty(trade, qty=2)

# Cancel order
api.cancel_order(trade)
```

### Order Status

```python
# Refresh all trade statuses from exchange
api.update_status(api.stock_account)
api.update_status(api.futopt_account)

# List all trades in current session
trades = api.list_trades()
```

### Order Callbacks

```python
# Traditional callback registration
api.set_order_callback(order_callback)

def order_callback(stat, msg):
    """
    stat: OrderState — order status enum
    msg: dict — order details including order_id, status, price, qty, etc.
    """
    pass

# Subscribe/unsubscribe trade updates
api.subscribe_trade()
api.unsubscribe_trade()
```

---

## 6. Quote Subscription

### Subscribe / Unsubscribe

```python
from shioaji import constant as sc

# Subscribe tick data (v1 format)
api.quote.subscribe(
    contract,
    quote_type=sc.QuoteType.Tick,
    version=sc.QuoteVersion.v1,
)

# Subscribe bid/ask data
api.quote.subscribe(
    contract,
    quote_type=sc.QuoteType.BidAsk,
    version=sc.QuoteVersion.v1,
)

# Subscribe quote (futures/options combined tick+bidask)
api.quote.subscribe(
    contract,
    quote_type=sc.QuoteType.Quote,
    version=sc.QuoteVersion.v1,
)

# Unsubscribe
api.quote.unsubscribe(
    contract,
    quote_type=sc.QuoteType.Tick,
    version=sc.QuoteVersion.v1,
)
```

### QuoteType & QuoteVersion

| Enum | Values | Description |
|------|--------|-------------|
| `QuoteType` | `Tick`, `BidAsk`, `Quote` | Data type to subscribe |
| `QuoteVersion` | `v0`, `v1` | Protocol version; `v1` is recommended |

### Callback Registration -- Decorator Style

```python
@api.on_tick_stk_v1()
def on_tick_stk(exchange, tick):
    """Stock tick callback (v1)."""
    # tick.close, tick.volume, tick.datetime, tick.simtrade
    pass

@api.on_bidask_stk_v1()
def on_bidask_stk(exchange, bidask):
    """Stock bid/ask callback (v1)."""
    # bidask.bid_price[0..4], bidask.ask_price[0..4]
    # bidask.bid_volume[0..4], bidask.ask_volume[0..4]
    pass

@api.on_tick_fop_v1()
def on_tick_fop(exchange, tick):
    """Futures/options tick callback (v1)."""
    pass

@api.on_bidask_fop_v1()
def on_bidask_fop(exchange, bidask):
    """Futures/options bid/ask callback (v1)."""
    pass

@api.on_quote_fop_v1()
def on_quote_fop(exchange, quote):
    """Futures/options combined quote callback (v1)."""
    pass
```

### Callback Registration -- Traditional Style

```python
api.quote.set_on_tick_stk_v1_callback(on_tick_stk)
api.quote.set_on_bidask_stk_v1_callback(on_bidask_stk)
api.quote.set_on_tick_fop_v1_callback(on_tick_fop)
api.quote.set_on_bidask_fop_v1_callback(on_bidask_fop)
api.quote.set_on_quote_fop_v1_callback(on_quote_fop)

# General event callback (connection events, errors)
api.set_event_callback(event_callback)

def event_callback(resp_code, event, info):
    pass
```

### Quote Event Listener

```python
@api.quote.on_event
def on_quote_event(resp_code, event, info):
    """Fires on quote connection events (disconnect, reconnect, etc.)."""
    pass
```

### Context Binding

```python
# Set context object accessible from callbacks
api.quote.set_context(my_context_obj)

# Or use bind=True in subscribe to auto-bind
api.quote.subscribe(contract, quote_type=sc.QuoteType.Tick, version=sc.QuoteVersion.v1, bind=True)
```

---

## 7. Historical Data

### Intraday Ticks

```python
from shioaji import TicksQueryType

# All ticks for the day
ticks = api.ticks(
    contract=api.Contracts.Stocks["2330"],
    date="2026-03-11",
    query_type=TicksQueryType.AllDay,
)

# Ticks in a time range
ticks = api.ticks(
    contract=api.Contracts.Stocks["2330"],
    date="2026-03-11",
    query_type=TicksQueryType.RangeTime,
    time_start="09:00:00",
    time_end="10:00:00",
)

# Last N ticks
ticks = api.ticks(
    contract=api.Contracts.Stocks["2330"],
    date="2026-03-11",
    query_type=TicksQueryType.LastCount,
    last_cnt=100,
)

# ticks.ts, ticks.close, ticks.volume, ticks.bid_price, ticks.ask_price, etc.
```

### Intraday KBars (1-min OHLCV)

```python
kbars = api.kbars(
    contract=api.Contracts.Stocks["2330"],
    date="2026-03-11",
)
# kbars.ts, kbars.Open, kbars.High, kbars.Low, kbars.Close, kbars.Volume
```

### Snapshots

```python
snapshots = api.snapshots([
    api.Contracts.Stocks["2330"],
    api.Contracts.Stocks["2317"],
])
# Returns list of Snapshot objects with OHLCV, bid/ask, total volume, etc.
```

---

## 8. Market Scanners

```python
from shioaji import constant as sc

scanners = api.scanners(
    scanner_type=sc.ScannerType.VolumeRank,
    count=50,     # Number of results
)
```

| ScannerType | Description |
|-------------|-------------|
| `ChangePercentRank` | Rank by price change percentage |
| `ChangePriceRank` | Rank by price change amount |
| `DayRangeRank` | Rank by intraday range |
| `VolumeRank` | Rank by volume |
| `AmountRank` | Rank by turnover amount |

---

## 9. Position & Account Queries

### Positions

```python
from shioaji import constant as sc

# Stock positions (by share count)
positions = api.list_positions(api.stock_account)
# Each position: code, direction, quantity, price, last_price, pnl

# Stock positions (by lot count)
positions = api.list_positions(api.stock_account, unit=sc.Unit.Share)

# Futures/options positions
positions = api.list_positions(api.futopt_account)

# Detailed position info
details = api.list_position_detail(api.stock_account)
```

### Profit & Loss

```python
# P&L summary
pnl = api.list_profit_loss(api.stock_account)

# Detailed P&L per trade
pnl_detail = api.list_profit_loss_detail(api.stock_account)

# P&L summary (aggregated)
pnl_summary = api.list_profit_loss_summary(api.stock_account)
```

### Account Balance & Margin

```python
# Account balance (stock account)
balance = api.account_balance()

# Margin info (futures/options account)
margin = api.margin(api.futopt_account)

# Trading limits (buying power)
limits = api.trading_limits(api.futopt_account)

# Settlement schedule
settlements = api.settlements(api.stock_account)
```

---

## 10. Market Info

```python
# Credit enquiry (margin trading / short selling availability)
credits = api.credit_enquires(api.Contracts.Stocks["2330"])

# Short stock sources
sources = api.short_stock_sources(api.Contracts.Stocks["2330"])

# Punish stocks (disposition stocks / 處置股)
punish_list = api.punish()

# Market notices / announcements
notices = api.notice()
```

---

## 11. Rate Limits

| Category | Limit | Window |
|----------|-------|--------|
| Connections | 5 per `person_id` | concurrent |
| Daily logins | 1,000 | per day |
| Quote queries (ticks/kbars/snapshots) | 50 | 5 seconds |
| Intraday ticks | 10 | 5 seconds |
| Intraday kbars | 270 | 5 seconds |
| Account queries | 25 | 5 seconds |
| Order operations (place/update/cancel) | 250 | 10 seconds |
| Quote subscriptions | 200 max | concurrent |

**Platform enforcement**: `order/rate_limiter.py` enforces order-side limits. Quote subscription count is tracked in `feed_adapter/shioaji/subscription_manager.py`.

---

## 12. Platform Integration Mapping

Mapping of Shioaji API methods to HFT platform modules.

| Shioaji API Method | Platform Module | Status |
|-------------------|----------------|--------|
| `sj.Shioaji()` | `feed_adapter/shioaji/facade.py` | Implemented |
| `login()` | `feed_adapter/shioaji/session_runtime.py` | Implemented |
| `logout()` | `feed_adapter/shioaji/session_runtime.py` | Implemented |
| `list_accounts()` | `feed_adapter/shioaji/account_gateway.py` | Implemented |
| `Contracts.Stocks/Futures/Options` | `feed_adapter/shioaji/contracts_runtime.py` | Implemented |
| `place_order()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `cancel_order()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `update_order()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `list_trades()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `update_status()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `quote.subscribe()` | `feed_adapter/shioaji/subscription_manager.py` | Implemented |
| `quote.unsubscribe()` | `feed_adapter/shioaji/subscription_manager.py` | Implemented |
| `set_on_tick_*_callback()` | `feed_adapter/shioaji/quote_runtime.py` | Implemented |
| `set_on_bidask_*_callback()` | `feed_adapter/shioaji/quote_runtime.py` | Implemented |
| `set_order_callback()` | `feed_adapter/shioaji/order_gateway.py` | Implemented |
| `set_event_callback()` | `feed_adapter/shioaji/quote_runtime.py` | Implemented |
| `ticks()` | Not wrapped (research use only) | N/A |
| `kbars()` | Not wrapped (research use only) | N/A |
| `snapshots()` | Not wrapped (research use only) | N/A |
| `scanners()` | Not wrapped | N/A |
| `list_positions()` | `feed_adapter/shioaji/account_gateway.py` | Implemented |
| `account_balance()` | `feed_adapter/shioaji/account_gateway.py` | Implemented |
| `margin()` | `feed_adapter/shioaji/account_gateway.py` | Implemented |
| `settlements()` | `feed_adapter/shioaji/account_gateway.py` | Implemented |

### Order Encoding/Decoding

Order translation between platform `OrderCommand` and Shioaji `Order` objects is handled by `feed_adapter/shioaji/order_codec.py`.

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
| Subscription lifecycle | Market Data | `feed_adapter/shioaji/subscription_manager.py` |
| Order codec (platform to Shioaji) | Execution | `feed_adapter/shioaji/order_codec.py` |
| Order placement/cancellation | Execution | `feed_adapter/shioaji/order_gateway.py` |
| Order rate limiting | Execution | `order/rate_limiter.py` |
| Fill routing | Execution | `execution/router.py` |
| Position tracking | Execution | `execution/positions.py` |
| Broker facade orchestration | Control | `feed_adapter/shioaji/facade.py` |

---

## 13. Key Environment Variables (HFT Platform)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_QUOTE_VERSION` | `auto` | Lock quote protocol: `auto` or `v1` |
| `HFT_SHIOAJI_SKIP_CERT` | `0` | `1` = skip CA cert (non-prod) |
| `HFT_MODE` | `sim` | `sim` uses simulation API |
| `HFT_BROKER` | `shioaji` | Broker backend: `shioaji` or `fubon` |
| `HFT_ORDER_MODE` | -- | Order routing mode |
| `HFT_ORDER_SIMULATION` | -- | Force simulation orders |
| `HFT_ORDER_NO_CA` | -- | Skip CA for orders |

---

## 14. Common Failure Modes

For deep diagnosis of reconnect, session refresh, and watchdog failures see `.agent/skills/shioaji-contracts/SKILL.md`.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CredentialError` | Wrong API key/secret | Check `.env` credentials |
| `CertificateError` | CA cert not found | `HFT_SHIOAJI_SKIP_CERT=1` or provide cert |
| `QuoteSchemaError` | Schema mismatch | `HFT_QUOTE_VERSION=auto` |
| Stale quotes | Network disruption | Auto-reconnect; check watchdog logs |
| Empty contracts | API timeout | Re-run `make sync-symbols` |
| `patch.object` fails on `SessionRuntime` | `__slots__` prevents instance dict | Use `patch.object(SessionRuntime, 'method')` not instance patching |
| Rate limit 429 | Too many requests | Respect limits in Section 11; check `rate_limiter.py` |
| Login timeout | Network / Shioaji server issues | Retry with exponential backoff; `session_runtime.py` handles this |
| Subscription cap exceeded | >200 concurrent subscriptions | Reduce symbol list or rotate subscriptions |

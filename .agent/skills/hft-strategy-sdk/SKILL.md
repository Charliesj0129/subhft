---
name: hft-strategy-sdk
description: Use when implementing a new runtime strategy, modifying an existing strategy's event handlers, or integrating features/signals into strategy logic. Covers BaseStrategy contract, hooks, order API, position tracking, gap resilience, and config patterns.
---

# HFT Strategy SDK Reference

Complete reference for implementing production strategies using `BaseStrategy`. This skill covers the SDK contract; use `hft-mm-design` for market-making patterns and `hft-hot-path-dev` for performance compliance.

## BaseStrategy Contract

```python
from hft_platform.strategy.base import BaseStrategy
from hft_platform.contracts.strategy import OrderIntent, Side, IntentType, TIF

class MyStrategy(BaseStrategy):
    __slots__ = ("_my_state",)  # REQUIRED: __slots__ on all hot-path classes

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self._my_state = kwargs.get("my_param", default_value)
```

## Event Hooks (All Optional)

| Hook | Event | When fired | Typical use |
|------|-------|-----------|-------------|
| `on_tick(TickEvent)` | Trade-by-trade | Each executed trade | Volume analysis, trade flow |
| `on_book_update(BidAskEvent)` | LOB update | Bid/ask change | Spread monitoring, L1 signals |
| `on_stats(LOBStatsEvent)` | LOB stats | After book update | Mid-price, spread, imbalance |
| `on_features(FeatureUpdateEvent)` | Feature plane | After LOB stats | ML features, regime detection |
| `on_fill(FillEvent)` | Own fill | Order filled | Position update, PnL tracking |
| `on_order(OrderEvent)` | Own order status | Status change | Cancel confirm, rejection |
| `on_gap(GapEvent)` | Bus overflow | Missed events | **MUST reset stale state** |
| `on_risk_feedback(RiskFeedback)` | Risk rejection | Pre-broker reject | Release pending counters |

### Hook Dispatch Flow

```
RingBufferBus event
  → StrategyRunner.process_event()
    → strategy.handle_event(ctx, event)
      → auto-filter by subscribed symbols
      → dispatch to on_tick/on_book_update/etc.
      → collect self._generated_intents
    → submit to RiskEngine via intent_queue
```

**Max intents per event**: 20 (`_MAX_INTENTS_PER_EVENT`)

## Order API

### Simple Methods

```python
# Buy: creates OrderIntent(side=BUY)
self.buy(symbol, price, qty, tif=TIF.LIMIT)

# Sell: creates OrderIntent(side=SELL)
self.sell(symbol, price, qty, tif=TIF.LIMIT)

# Cancel: targets existing order by ID
self.cancel(symbol, order_id)

# Read position: net qty for symbol
pos = self.position(symbol)  # int, 0 if no position
```

### Full API via StrategyContext

```python
intent = self.ctx.place_order(
    symbol="TMFD6",
    side=Side.BUY,
    price=225000000,              # scaled int x10000 (REQUIRED)
    qty=1,
    tif=TIF.LIMIT,               # LIMIT, IOC, FOK
    intent_type=IntentType.NEW,   # NEW, AMEND, CANCEL, FORCE_FLAT
    target_order_id="...",        # for AMEND/CANCEL
    price_type="LMT",            # "LMT" or "MKT"
)
```

### Price Contract (Precision Law)

```python
# CORRECT: scaled integer
self.buy("TMFD6", 225000000, 1)       # 22500 pts × 10000

# CORRECT: Decimal (auto-scaled by ctx)
from decimal import Decimal
self.buy("TMFD6", Decimal("22500"), 1)

# CORRECT: use ctx.scale_price()
price = self.ctx.scale_price("TMFD6", Decimal("22500.5"))

# FORBIDDEN: float (rejected in strict mode)
self.buy("TMFD6", 22500.0, 1)  # TypeError if HFT_STRICT_PRICE_MODE=1
```

## StrategyContext Read API

```python
# L1 order book (scaled integers)
l1 = self.ctx.get_l1_scaled(symbol)
if l1:
    ts_ns, bid, ask, mid_x2, spread_scaled, bid_depth, ask_depth = l1
    # bid, ask, mid_x2, spread_scaled are all x10000

# Feature engine (27 features in v3)
features = self.ctx.get_feature_tuple(symbol)
if features:
    # Indexed by feature registry IDs
    ofi_5s = features[idx_ofi_5s]

# Single feature
value = self.ctx.get_feature(symbol, "ofi_ema_5s")

# Feature staleness check
if self.ctx.is_feature_stale(symbol, max_age_ns=5_000_000_000):
    return  # Skip stale features

# Publish state for monitoring
self.ctx.publish_state("my_channel", {"signal": value})
```

## Position Tracking Pattern (R47 Best Practice)

```python
class MyStrategy(BaseStrategy):
    __slots__ = ("_local_pos", "_pending_buy", "_pending_sell", "_last_bid", "_last_ask")

    def __init__(self, strategy_id, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self._local_pos = {}       # Authoritative position (fill-tracked)
        self._pending_buy = {}     # Pending buy orders (not yet filled)
        self._pending_sell = {}    # Pending sell orders (not yet filled)
        self._last_bid = {}        # Last quoted bid (prevents ROD stack-up)
        self._last_ask = {}        # Last quoted ask

    def on_fill(self, event):
        sym = event.symbol
        if event.side == Side.BUY:
            self._local_pos[sym] = self._local_pos.get(sym, 0) + event.qty
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - event.qty)
        else:
            self._local_pos[sym] = self._local_pos.get(sym, 0) - event.qty
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - event.qty)

    def on_risk_feedback(self, feedback):
        # Release pending slot on risk rejection
        sym = feedback.symbol
        if feedback.side == Side.BUY:
            self._pending_buy[sym] = max(0, self._pending_buy.get(sym, 0) - 1)
            self._last_bid.pop(sym, None)   # Allow requote
        else:
            self._pending_sell[sym] = max(0, self._pending_sell.get(sym, 0) - 1)
            self._last_ask.pop(sym, None)

    def on_gap(self, event):
        # CRITICAL: clear pending on bus overflow — fills/cancels may be lost
        self._pending_buy.clear()
        self._pending_sell.clear()
        self._last_bid.clear()
        self._last_ask.clear()
```

### Price-Movement Gate (Prevent ROD Stack-Up)

```python
# ROD orders persist at exchange — resending same price stacks redundant orders
bid_moved = bid_price != self._last_bid.get(sym, -1)
if bid_moved and pos + pending < max_pos:
    self.buy(sym, bid_price, 1)
    self._pending_buy[sym] = self._pending_buy.get(sym, 0) + 1
    self._last_bid[sym] = bid_price
```

## Tick Grid Snapping

```python
PRICE_SCALE = 10000  # Tick size in scaled units (1 pt × 10000)

# Round DOWN for bid
bid_scaled = (bid_scaled // PRICE_SCALE) * PRICE_SCALE

# Round UP (ceiling) for ask
ask_scaled = -(-ask_scaled // PRICE_SCALE) * PRICE_SCALE
```

## Configuration

### strategies.yaml

```yaml
- id: "MY_STRATEGY"
  module: "hft_platform.strategies.my_strategy"
  class: "MyStrategy"
  enabled: true
  product_type: "FUT"           # FUT, OPT, STK
  symbols: ["TMFD6"]
  params:
    spread_threshold_pts: 5     # Points, not bps
    max_pos: 3
    feature_set_id: "lob_shared_v3"
```

### strategy_limits.yaml

```yaml
strategies:
  MY_STRATEGY:
    max_position_lots: 5
    max_order_qty: 1

intraday_pnl:
  scope: global
  soft_limit_ntd: 500
  hard_limit_ntd: 8000
  peak_drawdown_pct: 0.40
```

## Lifecycle Checklist

1. [ ] `__slots__` on strategy class
2. [ ] Prices as scaled int or Decimal (no floats)
3. [ ] `on_gap()` resets ALL mutable state
4. [ ] `on_risk_feedback()` releases pending counters
5. [ ] `on_fill()` updates local position tracking
6. [ ] Price-movement gate prevents ROD stack-up
7. [ ] Tick grid snapping on all prices
8. [ ] Feature staleness check before using features
9. [ ] Config uses points (not bps) for spread thresholds
10. [ ] Unit tests with scaled int assertions

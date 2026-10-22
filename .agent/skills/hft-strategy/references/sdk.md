# Strategy SDK reference

Read when implementing hooks. Verify signatures in `strategy/base.py` (the
`StrategyContext` class is defined there too).

## Hooks

| Hook | Event | Notes |
|---|---|---|
| `on_tick(TickEvent)` | each trade | flow, volume |
| `on_book_update(BidAskEvent)` | bid/ask change | L1 signals, quoting |
| `on_stats(LOBStatsEvent)` | after book update | mid, spread, imbalance |
| `on_features(FeatureUpdateEvent)` | feature plane | check quality flags |
| `on_fill(FillEvent)` / `on_order(OrderEvent)` | own fills / status | position, cancel confirm |
| `on_gap(GapEvent)` | bus overflow | must reset stale state |
| `on_risk_feedback(RiskFeedback)` | pre-broker reject | release pending counters |

## Orders

`self.buy(symbol, price, qty, tif=TIF.LIMIT)`, `self.sell(...)`,
`self.cancel(symbol, order_id)`, `self.position(symbol)` (net qty, cached), or
`self.ctx.place_order(symbol=..., side=Side.BUY, price=<scaled int>, qty=1,
tif=..., intent_type=IntentType.NEW, target_order_id=..., price_type="LMT")`.
Intent types: `NEW`, `AMEND`, `CANCEL`, `FORCE_FLAT`.

## Reads (all O(1))

`ctx.get_l1_scaled(symbol)` returns `(ts_ns, bid, ask, mid_x2, spread_scaled,
bid_depth, ask_depth)`; `ctx.get_feature(symbol, feature_id)`;
`ctx.get_feature_tuple(symbol)` (indexed by the feature registry in
`feature/registry.py`); `ctx.is_feature_stale(symbol, max_age_ns)`;
`ctx.scale_price(symbol, price)`; `ctx.publish_state(channel, payload)`.

## Position-tracking pattern (R47)

Keep a fill-tracked `_local_pos`, `_pending_buy`, `_pending_sell`, and the last
quoted bid/ask per symbol.

- `on_fill`: adjust position, decrement pending by `event.qty` (floor at 0).
- `on_risk_feedback`: decrement the pending counter for `feedback.side`, drop the
  last-quote entry so the strategy can requote.
- `on_gap`: clear all pending counters and last quotes.
- Quote only when the price moved (`bid != last_bid`) and `pos + pending < max_pos`.
- A position cap must never block its own unwind; reduce-only exits stay allowed.

## Config shapes

Registry entry (`config/live/strategies.yaml`): `id`, `module`, `class`,
`enabled`, `symbols`, `params`, `budget_us`, `required_feature_set_id`,
`required_feature_ids`. Limits (`config/base/strategy_limits.yaml`): per-strategy
`max_position_lots`, `max_order_qty`; global `intraday_pnl` soft/hard limits and
`peak_drawdown_pct`. Read the files; numbers here would go stale.

## Common failures

| Symptom | Check |
|---|---|
| risk rejects intent | float price; StormGuard state; exposure caps |
| backtest and live diverge | feature version, timestamp handling, latency profile |
| strategy never loads | registry path, module/class names, `enabled` |
| loop lag rises | allocation or blocking in a handler |
| circuit breaker trips | consecutive handler errors; missing data |
| position looks stale | PositionStore sync; fill callback chain |

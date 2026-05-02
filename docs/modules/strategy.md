# strategy — Strategy SDK & Runner

> **Package**: `src/hft_platform/strategy/`
> **Runtime Plane**: Decision
> **Hot-Path**: `StrategyRunner.process_event()`, `BaseStrategy.handle_event()`

## Overview

Event-driven strategy execution framework with dispatch, circuit breaker resilience, timeout protection, and feature compatibility management.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `base.py` | `BaseStrategy`, `StrategyContext` | Strategy SDK interfaces |
| `registry.py` | `StrategyRegistry`, `StrategyConfig` | YAML-based strategy configuration and instantiation |
| `runner.py` | `StrategyRunner` | Event dispatch, metrics, resilience patterns |
| `compat.py` | `check_strategy_feature_compat()` | Feature compatibility validation |

## BaseStrategy Interface

```python
class MyStrategy(BaseStrategy):
    def on_tick(self, event: TickEvent): ...
    def on_book_update(self, event: BidAskEvent): ...
    def on_stats(self, event: LOBStatsEvent): ...
    def on_features(self, event: FeatureUpdateEvent): ...
    def on_fill(self, event: FillEvent): ...
    def on_order(self, event: OrderEvent): ...
    def on_gap(self, event: GapEvent): ...          # Reset stale state
    def on_risk_feedback(self, feedback: RiskFeedback): ...
```

**Order placement**:
```python
self.buy(symbol, price=Decimal("600.5"), qty=1)
self.sell(symbol, price=155000, qty=1)  # Pre-scaled int
self.cancel(symbol, order_id="ABC123")
qty = self.position(symbol)
```

## StrategyContext

Read-only context passed to event handlers:

| Method | Returns | Purpose |
|--------|---------|---------|
| `get_l1_scaled(symbol)` | `(ts, bid, ask, mid_x2, spread, bd, ad)` | Fast L1 snapshot |
| `get_feature(symbol, id)` | `int \| float \| None` | Single feature value |
| `get_feature_tuple(symbol)` | `tuple` | All features as tuple |
| `is_feature_stale(symbol, max_age_ns)` | `bool` | Staleness check |
| `position(symbol)` | `int` | Current net position |
| `place_order(...)` | `OrderIntent` | Submit order (auto-scaling) |

## StrategyRunner

### Event Processing Pipeline

1. **Staleness guard** — skip events older than `HFT_STALE_EVENT_THRESHOLD_MS` (500ms)
2. **Position cache rebuild** — if marked dirty
3. **Strategy dispatch** — O(1) index for targeted or broadcast dispatch
4. **Per-strategy processing**:
   - Circuit breaker check
   - Quarantine check (StrategyHealthGovernor)
   - Timeout circuit check
   - `strategy.handle_event(ctx, event)` → `List[OrderIntent]`
5. **Intent processing**:
   - Populate `decision_price` from L1
   - Session phase filtering (TrackGate)
   - Cap at `HFT_MAX_INTENTS_PER_EVENT` (20)
   - Submit to risk_queue

### Circuit Breaker (3-State FSM)

```
Normal → Degraded (at half_threshold=5 failures)
Degraded → Halted (at full_threshold=10 failures)
Halted → Degraded (after 60s cooldown)
Degraded → Normal (after 5 consecutive successes)
```

Optional Rust acceleration via `RustCircuitBreaker`.

### Timeout Circuit Breaker

- Max handler time: `HFT_STRATEGY_TIMEOUT_MS` (50ms)
- Strikes before halt: `HFT_STRATEGY_TIMEOUT_STRIKES` (3)
- Recovery: `HFT_STRATEGY_TIMEOUT_RECOVER_S` (60s)
- GapEvent handlers exempt

## Configuration (strategies.yaml)

```yaml
strategies:
  - id: momentum_v1
    module: hft_platform.strategies.momentum_bounce
    class: MomentumBounce
    enabled: true
    budget_us: 200
    symbols: [TXFD6]
    product_type: FUTURES
    params:
      lookback_bars: 20
    required_feature_set_id: "lob_shared_v3"
    required_feature_schema_version: 3
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_STRATEGY_CONFIG` | `config/base/strategies.yaml` | Config path |
| `HFT_STRATEGY_CIRCUIT_THRESHOLD` | `10` | Failures to halt |
| `HFT_STRATEGY_CIRCUIT_COOLDOWN_S` | `60` | Cooldown before recovery |
| `HFT_STRATEGY_TIMEOUT_MS` | `50` | Max handler duration |
| `HFT_MAX_INTENTS_PER_EVENT` | `20` | Intent flood cap |
| `HFT_STALE_EVENT_THRESHOLD_MS` | `500` | Event staleness threshold |
| `HFT_STRICT_PRICE_MODE` | `1` | Reject float prices with TypeError |
| `HFT_DEFAULT_INTENT_TTL_MS` | `5000` | Intent time-to-live |

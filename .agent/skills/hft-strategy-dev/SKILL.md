---
name: hft-strategy-dev
description: Use when implementing or modifying live strategy code, handling runtime market events, emitting OrderIntent values, or integrating shared feature-plane signals into strategy logic.
---

# HFT Strategy Development

Use this skill for runtime strategy code under `src/hft_platform/strategy/` and `src/hft_platform/strategies/`. Keep research-only factor work in `hft-alpha-research`.

## Strategy Contract

- Inherit from `BaseStrategy`
- Override event hooks: `on_tick`, `on_book_update`, `on_stats`, `on_features`, `on_fill`, `on_order`
- Emit `OrderIntent` via `ctx.place_order()` only
- Prices must be scaled integers (x10000). Never pass floats.

## Minimal Blueprint

```python
class MyStrategy(BaseStrategy):
    __slots__ = ("threshold",)

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self.threshold = kwargs.get("threshold", 0)

    def on_book_update(self, event):
        l1 = self.ctx.get_l1_scaled(event.symbol)
        if not l1:
            return
        ts, bid, ask, mid_x2, spread, bid_depth, ask_depth = l1
        if spread > self.threshold:
            self.ctx.place_order(
                symbol=event.symbol,
                side=Side.BUY,
                price=bid + 1,  # scaled int
                qty=1,
                tif="ROD",
                price_type="LMT",
            )
```

## StrategyContext API

| Method | Returns | Hot-path safe |
| --- | --- | --- |
| `ctx.place_order(symbol, side, price, qty, tif, price_type, ...)` | `OrderIntent` | Yes |
| `ctx.get_l1_scaled(symbol)` | `(ts_ns, bid, ask, mid_x2, spread, bid_depth, ask_depth)` | Yes (O(1)) |
| `ctx.get_feature(symbol, feature_id)` | `int \| float \| None` | Yes |
| `ctx.get_feature_tuple(symbol)` | `tuple[...] \| None` | Yes (indexed access) |
| `ctx.position(symbol)` | `int` (net_qty) | Yes (cached) |

## Feature Engine v3 (27 features)

Access via `ctx.get_feature_tuple(symbol)` — returns indexed tuple:

```text
v1 [0-15]:  best_bid, best_ask, mid_price_x2, spread_scaled, bid/ask_depth,
            depth_imbalance_ppm, microprice_x2, l1_bid/ask_qty, l1_imbalance_ppm,
            ofi_l1_raw/cum/ema8, spread_ema8_scaled, depth_imbalance_ema8_ppm

v2 [16-21]: ofi_depth_norm_ppm, ret_autocov_5s_x1e6, tob_survival_ms,
            impact_surprise_x1000, deep_depth_momentum_x1000, toxicity_ema50_x1000

v3 [22-26]: ofi_l1_ema5s, ofi_l1_ema30s, imbalance_ema5s_ppm,
            spread_ema30s, spread_ema300s
```

Declare feature requirements in strategy config:
```yaml
strategies:
  - id: my_strategy
    required_feature_set_id: lob_shared_v3
    required_feature_ids: [ofi_l1_ema8, toxicity_ema50_x1000]
```

## Registration

1. Strategy class in `src/hft_platform/strategies/my_strategy.py`
2. Register in `config/base/strategies.yaml`:
```yaml
strategies:
  - id: my_strategy
    module: hft_platform.strategies.my_strategy
    class: MyStrategy
    enabled: true
    budget_us: 200
    symbols: [2330]
    params: {threshold: 50}
```
3. `StrategyRegistry.load()` + `.instantiate()` handles dynamic import

## 12 Strategy Implementations

### Core (7)
| Strategy | File | Type |
| --- | --- | --- |
| SimpleMarketMaker | `simple_mm.py` | Symmetric quotes + inventory skew |
| MMHawkes | `mm_hawkes.py` | Hawkes intensity spread adjustment |
| CascadeBounce | `cascade_bounce.py` | Orderflow cascade detection |
| OpportunisticMM | `opportunistic_mm.py` | Volatility-adjusted quoting |
| ElectronicEye | `electronic_eye.py` | TXO options MM (Guardian/Quoter/Hedger) |
| VPINRegimeSwitch | `vpin_regime_switch.py` | VPIN regime switching |
| RustAlpha | `rust_alpha.py` | Rust AlphaStrategy bridge |

### Alpha (5, in `strategies/alpha/`)
`alpha_ofi`, `alpha_hawkes`, `alpha_deep_hawkes`, `alpha_mhp`, `alpha_propagator`

## StrategyRunner Internals

- Per-strategy **circuit breaker**: 3-state FSM (normal -> degraded -> halted), 5 failures -> halt 60s, Rust-accelerated
- **Budget timeout**: `budget_us` soft limit per strategy cycle
- **Position cache**: synchronized from PositionStore at event dispatch
- **Typed intent fast-path**: tuple format avoids OrderIntent allocation on hot path

## Hot-Path Rules

- No allocations inside event handlers (use pre-allocated buffers)
- No blocking I/O, no `time.sleep()`, no `pandas`
- Use `timebase.now_ns()` for timestamps (never `datetime.now()`)
- Use `structlog`, not `print()`
- Add `__slots__` to hot-path classes
- Keep `handle_event` under budget_us (default 200us)

## Common Failure Patterns

| Symptom | Action |
| --- | --- |
| Risk rejects intent | Check price type (float -> REJECT), check StormGuard state |
| Backtest/live diverge | Verify shared feature version + timestamp handling |
| Strategy never loads | Check registry.py wiring + YAML config |
| Loop lag rises | Remove allocations, blocking work from handlers |
| Circuit breaker trips | Check consecutive errors, verify external data availability |
| Position stale | Check PositionStore sync, verify fill callback chain |

## Testing

```bash
uv run hft strat test --symbol 2330 --strategy-id my_strategy
make test-file FILE=tests/unit/test_strategy_runner_behavior.py
```

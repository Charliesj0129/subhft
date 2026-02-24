# strategy

## Purpose

Strategy SDK, dispatch, and context utilities. This is the main module agents modify when implementing new strategies.

## Key Files

- `src/hft_platform/strategy/base.py`: `BaseStrategy` (abstract) + `StrategyContext` (SDK).
- `src/hft_platform/strategy/runner.py`: `StrategyRunner` — event dispatch, circuit breaker, metrics (515 lines).
- `src/hft_platform/strategy/registry.py`: `StrategyRegistry` — load strategies from YAML config.
- `src/hft_platform/strategy/factors.py`: Feature wiring helpers.
- `src/hft_platform/strategy/cli.py`: Optional strategy CLI helpers.

## How to Write a New Strategy

```python
from hft_platform.strategy.base import BaseStrategy, StrategyContext

class MyAlpha(BaseStrategy):
    strategy_id = "my_alpha"
    symbols = {"2330"}  # or use tag: "tag:futures|etf"

    def handle_event(self, ctx: StrategyContext, event) -> list:
        # ctx.positions = {symbol: net_qty}
        # ctx.place_order(symbol, side, price, qty)  → builds OrderIntent
        # ctx.get_l1_scaled(symbol) → (ts_ns, bid, ask, mid_x2, spread, ...)
        return []  # Return list of OrderIntent
```

Register in `config/base/strategies.yaml`:

```yaml
strategies:
  - module: src.hft_platform.strategies.my_alpha
    class: MyAlpha
    enabled: true
```

## StrategyRunner Internals

- **Circuit Breaker**: 3-state FSM (normal→degraded→halted). `HFT_STRATEGY_CIRCUIT_THRESHOLD=10`.
- **Typed Intent**: Zero-alloc tuple path when `HFT_TYPED_INTENT_CHANNEL=1`.
- **Metrics Batching**: Controlled by `HFT_OBS_POLICY` (minimal/balanced/debug).
- **Position Cache**: Dirty-flag + snapshot to avoid concurrent mutation from broker threads.
- **Symbol Tags**: Strategies can use `symbols = {"tag:futures"}` for dynamic subscription.

## Inputs and Outputs

- **Inputs**: `TickEvent`, `BidAskEvent`, `LOBStatsEvent` via `RingBufferBus`.
- **Outputs**: `OrderIntent` list → `risk_queue` or `LocalIntentChannel`.

## Testing

```bash
make test  # Runs all unit tests including strategy tests
uv run hft strat test --symbol 2330  # Test specific strategy
```

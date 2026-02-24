# execution

## Purpose

Normalize broker execution events, maintain positions, and reconcile state.

## Key Files

| File                          | Key Class             | Lines                                       |
| ----------------------------- | --------------------- | ------------------------------------------- |
| `execution/normalizer.py`     | `ExecutionNormalizer` | Normalizes broker fill/order payloads       |
| `execution/positions.py`      | `PositionStore`       | Integer-only position tracking (~280 lines) |
| `execution/router.py`         | `ExecutionRouter`     | Routes fills → PositionStore → EventBus     |
| `execution/reconciliation.py` | `Reconciler`          | Broker vs local state reconciliation        |
| `execution/gateway.py`        | Thin wrapper          | Delegates to OrderAdapter                   |

## PositionStore

- **Integer arithmetic only**: PnL, avg_price, net_qty all use scaled ints. NO float.
- **Dual call paths**: `on_fill()` (sync, broker thread) and `on_fill_async()` (event loop). Both update same state.
- **Rust implementation**: Optional `RustPositionTracker` from `rust_core` (~10x faster).
- **Key format**: `"pos:{strategy_id}:{symbol}"` → `PositionEntry(net_qty, avg_price_scaled, realized_pnl)`.

## Data Flow

```
Broker callback → ExecutionNormalizer.normalize() → FillEvent/OrderEvent
→ ExecutionRouter.process() → PositionStore.on_fill()
→ PositionDelta → EventBus.publish() → StrategyRunner (invalidates position cache)
```

## Gotchas

- `PriceCodec` is used for consistent scaling — must match normalizer's codec.
- Strategy attribution uses `OrderIdResolver` to map broker order IDs to strategy intents. If mapping fails, fill is "orphaned".
- `on_fill()` runs in broker thread — do NOT access async resources from it.

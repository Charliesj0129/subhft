# contracts

## Purpose

Shared data structures defining the module boundaries for strategy→risk→order→execution pipeline.

## Key Files

- `src/hft_platform/contracts/strategy.py`: Strategy-side contracts.
- `src/hft_platform/contracts/execution.py`: Execution-side contracts.

## Strategy Contracts (`contracts/strategy.py`)

| Type              | Purpose                                         |
| ----------------- | ----------------------------------------------- |
| `OrderIntent`     | Strategy output — "I want to buy/sell X"        |
| `OrderCommand`    | Risk-approved intent — ready for broker         |
| `RiskDecision`    | Risk engine output (approved/rejected + reason) |
| `IntentType`      | Enum: NEW, AMEND, CANCEL                        |
| `Side`            | Enum: BUY, SELL                                 |
| `TIF`             | Enum: IOC, ROD, FOK                             |
| `StormGuardState` | Enum: NORMAL(0), WARM(1), STORM(2), HALT(3)     |

## Execution Contracts (`contracts/execution.py`)

| Type            | Purpose                      |
| --------------- | ---------------------------- |
| `FillEvent`     | Broker fill confirmation     |
| `OrderEvent`    | Broker order status update   |
| `PositionDelta` | Position change notification |

## Data Flow

```
Strategy → OrderIntent → RiskEngine → RiskDecision
  → (if approved) → OrderCommand → OrderAdapter → Broker
  → Broker → FillEvent → ExecutionRouter → PositionStore → PositionDelta
```

## Gotchas

- `OrderIntent.price` is a **scaled integer** (x10000). Never pass float/Decimal directly.
- Typed intent fast path uses tuples tagged `"typed_intent_v1"` instead of `OrderIntent` objects.
- Keep changes backward compatible — these are the module boundary contracts.

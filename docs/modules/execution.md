# execution

## Purpose
Normalize broker execution events and maintain positions.

## Key Files
- `src/hft_platform/execution/normalizer.py`: `ExecutionNormalizer` for order/fill events.
- `src/hft_platform/execution/positions.py`: `PositionStore` and position deltas.
- `src/hft_platform/execution/reconciliation.py`: Reconcile broker state with local state.

## Inputs and Outputs
- Input: broker raw order/fill events.
- Output: `OrderEvent`, `FillEvent`, and position updates.

## Notes
- Uses `PriceCodec` for consistent scaling.
- Strategy attribution relies on `OrderIdResolver` and custom fields.

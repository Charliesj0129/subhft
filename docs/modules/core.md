# core

## Purpose
Low-level utilities shared across modules.

## Key Files
- `src/hft_platform/core/order_ids.py`: Resolve broker order IDs to strategy intents.
- `src/hft_platform/core/pricing.py`: Price scaling (`PriceCodec`, scale providers).

## Key Concepts
- **OrderIdResolver**: Map broker identifiers to `strategy_id:intent_id` keys.
- **PriceCodec**: Central scaling for prices across market data, risk, and execution.

## Notes
- Price scaling consistency is critical; use `SymbolMetadataPriceScaleProvider`.

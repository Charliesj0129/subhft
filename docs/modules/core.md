# core

## Purpose

Low-level utilities shared across all modules. These are foundational — used everywhere.

## Key Files

| File                      | Key Exports                                      | Purpose                                                                       |
| ------------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------- |
| `core/timebase.py`        | `now_ns()`, `now_s()`, `TZ_NAME`                 | Monotonic-aligned timestamps. **NEVER use `datetime.now()` or `time.time()`** |
| `core/pricing.py`         | `PriceCodec`, `SymbolMetadataPriceScaleProvider` | Decimal↔scaled-int conversion                                                 |
| `core/market_calendar.py` | `is_trading_hours()`, `next_open()`              | TWSE trading hours (09:00-13:30 TW, half-day calendar)                        |
| `core/order_ids.py`       | `OrderIdResolver`                                | Map broker order IDs to `strategy_id:intent_id` keys                          |

## PriceCodec Usage

```python
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider

codec = PriceCodec(SymbolMetadataPriceScaleProvider(symbol_metadata))
scaled = codec.scale("2330", Decimal("595.00"))  # → 5950000
price = codec.unscale("2330", 5950000)            # → Decimal("595.00")
```

## Timebase Usage

```python
from hft_platform.core import timebase

ts_ns = timebase.now_ns()    # Monotonic-aligned nanoseconds
ts_s = timebase.now_s()      # Monotonic-aligned seconds (float)
```

## Gotchas

- `PriceCodec` caches scale lookups per symbol. Symbol metadata changes require creating a new codec or calling reload.
- `market_calendar` handles TWSE-specific holidays. Not applicable to other exchanges without modification.
- `OrderIdResolver` is critical for fill attribution — if it fails, fills become "orphaned" with no strategy attribution.

# engine

## Purpose
Core runtime primitives (event bus).

## Key Files
- `src/hft_platform/engine/event_bus.py`: Ring buffer event bus.

## Responsibilities
- Fan-out market data and derived events to consumers.
- Provide a consistent publish API for services and strategies.

## Notes
- The bus is used by `MarketDataService` and the recorder pipeline.

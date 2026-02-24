# engine

## Purpose

Core runtime event bus using Disruptor-pattern ring buffer.

## Key Files

- `src/hft_platform/engine/event_bus.py`: `RingBufferBus` — main event distribution.

## RingBufferBus

- **Pattern**: SPMC (single producer, multiple consumer) ring buffer inspired by LMAX Disruptor.
- **Rust fast path**: Uses `rust_core.FastRingBuffer` when available. Falls back to Python deque.
- **API**:
  - `publish_nowait(event)` — non-blocking publish (hot path, used by MarketDataService)
  - `publish_many_nowait(events)` — batch publish (event + LOBStats pair)
  - `consume()` → async generator (used by StrategyRunner)
  - `consume_batch(n)` → async batch generator (opt-in via `HFT_BUS_BATCH_SIZE`)

## Configuration

| Variable             | Default | Purpose                               |
| -------------------- | ------- | ------------------------------------- |
| `HFT_BUS_CAPACITY`   | `8192`  | Ring buffer capacity (power of 2)     |
| `HFT_BUS_BATCH_SIZE` | `0`     | Batch consume size (0 = single event) |

## Gotchas

- `publish_nowait()` drops events if buffer is full — monitor `bus_overflow_total` metric.
- Consumers share the same buffer. Slow consumers can cause head-of-line blocking.
- The bus is NOT persisted. Use recorder pipeline for durable storage.

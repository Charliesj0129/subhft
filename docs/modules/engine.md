# engine — RingBufferBus Event Routing

> **Package**: `src/hft_platform/engine/`
> **Runtime Plane**: Market Data / Decision
> **Hot-Path**: Yes (lock-free publish, per-event consume)

## Overview

LMAX Disruptor-pattern event bus with lock-free single-writer ring buffer, multi-reader consumption, and optional Rust acceleration. Routes all events between market data, strategy, and recording planes.

## Architecture

```
MarketDataService → publish_nowait(event) → [RingBuffer 65536] → consume() → StrategyRunner
                                                                 → consume() → RecorderService
```

## Key Class: RingBufferBus

```python
bus = RingBufferBus(size=65536, storm_guard=sg)
bus.publish_nowait(event)           # Lock-free sync publish
await bus.publish(event)            # Async with optional write lock
async for event in bus.consume():   # Per-consumer async generator
    process(event)
```

### Operating Modes

| Mode | Env | Description |
|------|-----|-------------|
| `python` | `HFT_BUS_MODE=python` | Pure Python deque (default) |
| `rust_pyobj` | `HFT_BUS_MODE=rust_pyobj` | Rust ring buffer storing Python objects |
| `rust_typed` | `HFT_BUS_MODE=rust_typed` | Rust typed ring buffers per event type |

### Typed Ring Specializations

| Ring | Env | Purpose |
|------|-----|---------|
| `FastTickRingBuffer` | `HFT_BUS_TYPED_TICK_RING=1` | Compact tick tuple storage |
| `FastBidAskRingBuffer` | `HFT_BUS_TYPED_BOOK_RINGS=1` | Packed bid/ask storage (configurable levels) |
| `FastLOBStatsRingBuffer` | (auto with typed) | Fixed-width LOB stats |

### Overflow Handling

1. Consumer lag > buffer size → **GapEvent** injected, consumer skips ahead
2. Consecutive overflows > threshold → **StormGuard HALT** triggered
3. Overflow rate > threshold in sliding window → **StormGuard HALT**

### Wait Modes

| Mode | Env | Latency | CPU |
|------|-----|---------|-----|
| `event` | `HFT_BUS_WAIT_MODE=event` | Higher | Low (async signals) |
| `spin` | `HFT_BUS_WAIT_MODE=spin` | Lower | High (busy-wait) |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_BUS_MODE` | `python` | Bus operating mode |
| `HFT_BUS_WAIT_MODE` | `event` | Consumer notification strategy |
| `HFT_BUS_SINGLE_WRITER` | `1` | Skip write lock (single-writer fast path) |
| `HFT_BUS_NOTIFY_EVERY` | `1` | Batch notification frequency |
| `HFT_BUS_OVERFLOW_HALT_THRESHOLD` | `3` | Consecutive overflows before HALT |
| `HFT_BUS_OVERFLOW_RATE_THRESHOLD` | `10` | Overflows in window before HALT |
| `HFT_BUS_OVERFLOW_WINDOW_S` | `60` | Sliding window for rate tracking |
| `HFT_BUS_TYPED_TICK_RING` | `0` | Enable tick ring specialization |
| `HFT_BUS_TYPED_BOOK_RINGS` | `0` | Enable bid/ask ring specialization |
| `HFT_BUS_TYPED_BIDASK_PACKED_LEVELS` | `5` | Max book levels for packing |
| `HFT_BUS_SPIN_BUDGET` | `100` | Spin iterations before yield |

## Metrics

- `bus_overflow_total` — Total overflow events
- `bus_gap_events_total` — Total GapEvents injected

## Gotchas

1. **Single-writer assumption**: Default `HFT_BUS_SINGLE_WRITER=1` — multiple async publishers will race unless set to `0`
2. **GapEvent ts**: Uses `now_ns()` (current time), NOT the missed events' timestamps
3. **Strategies must re-sync on GapEvent** (e.g., re-request LOB snapshot)
4. **Consumer signal cleanup**: Per-consumer `asyncio.Event` auto-unregistered on generator close; crash without cleanup = minor leak

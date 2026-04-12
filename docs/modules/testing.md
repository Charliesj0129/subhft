# testing — Load & Fault Testing

> **Package**: `src/hft_platform/testing/`
> **Files**: 3

## Overview

Load testing, fault injection, and shadow execution for system validation.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `load_generator.py` | `LoadGenerator` | Synthetic market data generation |
| `fault_injector.py` | `FaultInjector` | Controlled fault injection |
| `shadow_runner.py` | `ShadowRunner` | Shadow execution against live data |

## LoadGenerator

Generates synthetic tick/bidask events for performance testing:

```python
gen = LoadGenerator(symbols=["TXFD6"], tick_rate=100)
async for event in gen.stream():
    bus.publish_nowait(event)
```

## FaultInjector

Controlled fault injection for resilience testing:
- Network disconnection simulation
- Queue overflow injection
- ClickHouse failure simulation

## ShadowRunner

Runs strategies against live data without real order execution:
- Mirrors production event flow
- Records hypothetical intents
- Measures signal quality

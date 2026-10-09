---
name: hft-hot-path-dev
description: "Guard rails for editing the tick-to-order hot path (normalizer, LOB engine, feature engine, event bus, strategy runner, risk, order adapter). Use when modifying or reviewing code between the broker callback and OrderAdapter.place_order. Not for off-path code such as the recorder or research."
---

# Hot-path development

The laws and the reject-on-sight list are in `AGENTS.md`. This skill adds what
the laws do not say: where the path is, which files bite, and how to verify.

```
ShioajiClient callback -> raw_queue -> MarketDataService -> Normalizer -> LOBEngine
 -> FeatureEngine -> RingBufferBus -> StrategyRunner -> RiskEngine -> OrderAdapter
```

## Files that need extra scrutiny

| File | Concern |
|---|---|
| `feed_adapter/normalizer.py` | scaled-int output; Rust and Python paths stay in parity |
| `feed_adapter/lob_engine.py` | preallocated book arrays; stats without allocation |
| `feature/engine.py` | warmup guard; no allocation per LOB-stats update |
| `strategy/runner.py` | dispatch latency; no blocking in event processing |
| `risk/engine.py` | RiskFeedback completeness; pending-counter integrity |
| `order/adapter.py` | queue coalescing; monotonic `deadline_ns` |
| `engine/event_bus.py` | `publish_nowait`; overflow policy |

Per-tick hazards that are easy to miss: list/dict/f-string/object creation in a
loop, a missing `__slots__` on a hot class, `json.loads` on large payloads
(use `orjson`), `.tolist()` or list comprehensions over a Rust result. Time
comes from `from hft_platform.core.timebase import now_ns`, never `datetime.now()`.

## Performance checklist

- O(1) lookups in tick loops; no scans. Views and zero-copy FFI instead of copies.
- Event-loop lag budget is 1 ms; CPU-heavy Rust releases the GIL.
- Avoiding GC during active trading needs explicit lifecycle handling.
- CPU isolation or kernel-bypass experiments need explicit verification and docs.

## Verify

```bash
make discipline            # AST rules HFT-D/A/P/S: silent except, SDK leakage, hot-path time/pandas/requests/print
make dependency-boundary
make lint
make test-file FILE=tests/unit/test_normalizer.py
make benchmark             # latency regression when the change is latency-relevant
make hotpath-profile       # per-stage latency profile
```

## Done when

`make discipline`, `make dependency-boundary`, and the targeted tests pass; the
change has tests for scaled-int values, monotonic time, and the Rust-failure
fallback; and a latency-relevant change has benchmark evidence.

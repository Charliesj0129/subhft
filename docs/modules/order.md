# order — Order Adapter & Lifecycle

> **Package**: `src/hft_platform/order/`
> **Runtime Plane**: Execution
> **Hot-Path**: `OrderAdapter._api_queue.put_nowait()`

## Overview

Order dispatch and lifecycle management: rate limiting, 5ms coalescing, per-strategy circuit breaker, shadow mode, and dead-letter queue (DLQ).

## Files (6)

| File | Key Exports | Purpose |
|------|-------------|---------|
| `adapter.py` | `OrderAdapter` | Core order dispatch (53KB, largest module) |
| `circuit_breaker.py` | `CircuitBreaker` | Per-strategy order circuit breaker |
| `dead_letter.py` | `DeadLetterQueue` | Failed order DLQ with TTL (30s) |
| `halt_canceller.py` | `HaltCanceller` | Auto-cancel outstanding orders on HALT |
| `shadow_sink.py` | `ShadowOrderSink` | Shadow mode order logging |
| `__init__.py` | — | Package exports |

## OrderAdapter

Core order dispatch engine:

```python
adapter = OrderAdapter(client, risk_engine, position_store, ...)
await adapter.run()  # Consumes from _api_queue
```

### Rate Limiting
- Global: 180 soft / 250 hard per 10-second window
- Per-symbol: configurable soft/hard caps
- Violation → order rejected, metric incremented

### Order Coalescing
- 5ms window for combining multiple intents to same symbol
- Reduces broker API calls under burst conditions
- Configurable via `HFT_ORDER_COALESCE_MS`

### Per-Strategy Circuit Breaker
- Tracks consecutive failures per strategy
- Threshold → disable strategy's orders temporarily
- Cooldown before recovery

### Shadow Mode
- `HFT_ORDER_SHADOW_MODE=1` → log orders without submitting to broker
- `ShadowOrderSink` captures full order details
- Used for paper trading and validation

### DLQ (Dead Letter Queue)
- Failed orders held for 30s TTL
- Periodic retry attempts
- Metrics: `order_dlq_size`, `order_dlq_retries`

## Order Flow

```
OrderCommand → _api_queue (bounded) → OrderAdapter.run()
  → Rate limit check
  → Coalescing window
  → Circuit breaker check
  → Shadow mode check
  → Broker API (place_order / cancel_order / update_order)
  → TCA stamp (arrival_price)
  → Metrics + recording
```

## HaltCanceller

On StormGuard HALT:
1. Iterates all outstanding orders
2. Submits CANCEL for each
3. Tracks cancellation results
4. Reports failures via metrics

## Execution Pipeline (10 steps)

```
1. HALT State Check → blocks NEW unless exempt
2. Idempotency Dedup (D-01) → skip if CANCEL/FORCE_FLAT
3. Per-Symbol Rate Limit (WU-06) → HARD → DLQ
4. Per-Strategy Circuit Breaker (WU-09) → blocks if open
5. Global Circuit Breaker → blocks if open
6. Global Rate Limit → blocks if hard cap exceeded
7. Platform Degrade Check → reduce-only enforcement
8. Shadow Mode Intercept (WU-10) → log, skip broker
9. Client Validation → hasattr checks
10. Queue vs Direct Dispatch → coalescing in _api_worker
```

## Phantom Order Tracking (D-03)

When a mutating API call (place_order, update_order) times out:
1. Add to `_phantom_order_keys` — order may have succeeded at broker
2. Return None (order state unknown)
3. Reconciliation responsible for confirming resolution
4. Eviction: entries >1 hour old auto-evicted when dict exceeds 1000

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_ORDER_SHADOW_MODE` | `0` | Enable shadow mode (no real orders) |
| `HFT_API_COALESCE_WINDOW_S` | `0.005` | 5ms coalescing window |
| `HFT_API_TIMEOUT_S` | `3.0` | Broker API call timeout |
| `HFT_API_MAX_INFLIGHT` | `16` | Concurrent API calls |
| `HFT_API_QUEUE_MAX` | `1024` | Max pending commands |
| `HFT_STRATEGY_CB_THRESHOLD` | `5` | Per-strategy CB threshold |
| `HFT_STRATEGY_CB_TIMEOUT_S` | `60` | Per-strategy CB timeout |
| `HFT_DLQ_DIR` | `.dlq` | Dead letter queue directory |

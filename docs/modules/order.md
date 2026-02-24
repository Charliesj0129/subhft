# order

## Purpose

Outbound order management — rate limiting, circuit breaking, and broker API interaction.

## Key Files

| File                       | Key Class              | Purpose                                  |
| -------------------------- | ---------------------- | ---------------------------------------- |
| `order/adapter.py`         | `OrderAdapter`         | Main order lifecycle manager (598 lines) |
| `order/rate_limiter.py`    | `SlidingWindowLimiter` | Per-session rate limiting                |
| `order/circuit_breaker.py` | `CircuitBreaker`       | Broker API failure guard                 |

## OrderAdapter Flow

```
OrderCommand → _api_queue.get()
  → rate_limiter.check() → reject if limit hit
  → circuit_breaker.check() → reject if open
  → ShioajiClient.place_order() / amend_order() / cancel_order()
  → broker_id_map[ordno] = intent_id  (for fill attribution)
  → on failure → DLQ (_dead_letters list)
```

## Features

- **Dead Letter Queue**: Failed orders go to `_dead_letters`. Inspect via CLI `hft dlq`.
- **Retry Logic**: Transient broker errors are retried up to N times with backoff.
- **Broker ID Mapping**: `_broker_id_map` tracks ordno↔intent_id for `ExecutionRouter` fill attribution.
- **Sim Mode**: `HFT_ORDER_MODE=sim` returns fake fills without calling broker.

## Configuration

| Variable                      | Default            | Purpose                                  |
| ----------------------------- | ------------------ | ---------------------------------------- |
| `HFT_ORDER_MODE`              | follows `HFT_MODE` | `sim` = fake fills, `live` = real broker |
| `HFT_ORDER_RATE_LIMIT`        | `10`               | Max orders per sliding window            |
| `HFT_ORDER_RATE_WINDOW_S`     | `1`                | Sliding window duration                  |
| `HFT_ORDER_CIRCUIT_THRESHOLD` | `5`                | Consecutive failures to open circuit     |

## Gotchas

- `HFT_ORDER_MODE=sim` is the most common reason "orders aren't going through" — check mode FIRST.
- `_api_queue` is an asyncio.Queue; if full, gateway commits a REJECTED dedup entry.
- Circuit breaker is separate from strategy circuit breaker (in StrategyRunner). Both can independently halt order flow.

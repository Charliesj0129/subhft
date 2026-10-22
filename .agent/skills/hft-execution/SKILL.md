---
name: hft-execution
description: "Execution-plane guide for order dispatch, circuit breaker, dead-letter queue, fill routing, positions, reconciliation, and the gateway (exposure, idempotency). Use when editing order/, execution/, or gateway/. Not for broker SDK adapters, which are broker-integration, or for risk validators."
---

# Execution plane

Path from `OrderCommand` to `PositionDelta`. Laws, red lines, and the HALT rule
are in `AGENTS.md`.

```
OrderCommand -> order_queue -> OrderAdapter [deadline -> rate limit -> circuit breaker -> 5 ms coalesce]
  -> client.place_order (blocking; live_orders + TCA maps registered)
broker callback -> raw_exec_queue -> ExecutionRouter
  -> normalizer: OrderEvent / FillEvent (TCA enrichment) -> PositionStore -> [PositionDelta, FillEvent] -> bus + recorder
```

## Modules

| Module | Role |
|---|---|
| `order/adapter.py` `OrderAdapter` | dispatch; rate limit soft 180 / hard 250 per 10 s; coalescing (`HFT_API_COALESCE_WINDOW_S`, 5 ms); deadline check; shadow mode |
| `order/circuit_breaker.py` | per-strategy FSM normal -> degraded -> halted |
| `order/deadletter.py` | holds orders when the queue is full (TTL, drained periodically) |
| `order/halt_canceller.py` | batch-cancel live orders on HALT |
| `order/shadow*.py` | dry-run sink and ClickHouse writer |
| `execution/router.py` `ExecutionRouter` | fill ingestion loop |
| `execution/normalizer.py` | broker callback -> OrderEvent/FillEvent; strategy-id resolution |
| `execution/positions.py` `PositionStore` | integer-only PnL, Rust O(1) tracker |
| `execution/reconciliation.py`, `startup_recon.py`, `checkpoint.py`, `mtm.py` | 3-way reconciliation, boot recovery, snapshots, mark-to-market |
| `execution/fill_dlq.py` | fills with `strategy_id="UNKNOWN"`, retried by a resolver |
| `gateway/` (`HFT_GATEWAY_ENABLED=1`) | `GatewayService` synchronous dispatch, `IdempotencyStore`, `ExposureStore`, `GatewayPolicy`, leader lease |

## Rules that bite

1. Prices are scaled int x10000; no float in position or PnL math. Closing PnL =
   `(exit - entry) * close_qty * multiplier` when signs differ; opening uses a weighted average.
2. `CANCEL` and `FORCE_FLAT` always pass HALT and are exempt from rate limiting.
3. Strategy-id resolution: custom field -> `order_id_map` -> `"UNKNOWN"` -> fill DLQ.
   Every `_add_to_dlq` path needs a matching release, or a maker freezes.
4. TCA maps and `order_id_map` are bounded (10,000, FIFO eviction).
5. Check HALT in the API worker, not only at `execute()` (TOCTOU). Do not record
   circuit-breaker failures during quarantine. A fill must finish the order
   exactly once; release pending exactly once.
6. A position cap must never block its own unwind (reduce-only is allowed through).
7. `HFT_ORDER_MODE=sim` still dispatches; only `disabled` stops orders.

Known incident shapes: `.agent/memory/module_gotchas.md` and `failed-attempts.md`.

## Verify

```bash
make test-file FILE=tests/unit/test_execution_router_loop.py
make test-file FILE=tests/unit/test_order_adapter_safety.py
make test-file FILE=tests/unit/test_position_store_unit.py
make shioaji-guard      # if the broker boundary was touched
```

## Done when

Tests above pass plus the HFT cases (scaled ints, HALT with cancel allowed, fail-closed,
one pending release per fill). A change to dispatch or risk interplay gets an
independent review.

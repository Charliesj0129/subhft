---
name: hft-execution
description: Use when working on the execution plane — fill processing, position tracking, reconciliation, execution optimization, slippage, TCA, or any code in order/, execution/, or gateway/.
---

# HFT Execution Plane

Use this skill for anything in `order/`, `execution/`, or `gateway/`. This is the path from OrderCommand to PositionDelta.

## Module Map (27 files across 3 packages)

### order/ (6 files)
| File | Class | Purpose |
| --- | --- | --- |
| `adapter.py` (53KB) | `OrderAdapter` | Main dispatch: rate limit (180/250 per 10s), 5ms coalescing, 16 concurrent, deadline check, shadow mode |
| `circuit_breaker.py` | `StrategyCircuitBreakerManager` | Per-strategy 3-state FSM: normal -> degraded -> halted (5 failures, 60s timeout) |
| `deadletter.py` | `DeadLetterQueue` | Hold orders on queue full, TTL 30s, drain every 50 events |
| `halt_canceller.py` | `HaltCanceller` | Batch cancel all live orders on HALT |
| `shadow.py` | `ShadowOrderSink` | Dry-run tracking without API submission |
| `shadow_writer.py` | `ShadowWriter` | Shadow orders -> ClickHouse hft.shadow_orders |

### execution/ (15 files)
| File | Class | Purpose |
| --- | --- | --- |
| `router.py` (15KB) | `ExecutionRouter` | Main fill ingestion loop: normalize -> positions -> publish -> record |
| `gateway.py` | `ExecutionGateway` | Execution wrapper + liveness metrics |
| `normalizer.py` | `ExecutionNormalizer` | Broker callback -> OrderEvent/FillEvent (strategy ID resolution chain) |
| `positions.py` (15KB) | `PositionStore` | Integer-only PnL: closing=(exit-entry)*qty*mult, opening=weighted avg. Rust O(1) tracker |
| `reconciliation.py` | `ReconciliationService` | 3-way broker/local/CH reconciliation, can trigger HALT |
| `startup_recon.py` | `StartupPositionVerifier` | Boot-time position recovery from broker + checkpoint |
| `eod_recon.py` | `EodReconciliation` | End-of-day reconciliation |
| `checkpoint.py` | `PositionCheckpoint` | Periodic snapshot to ClickHouse hft.pnl_snapshots |
| `execution_optimizer.py` | `ExecutionOptimizer` | Limit vs Market decision based on LOB queue depth (Albers 2025) |
| `imbalance_timer.py` | `ImbalanceTimer` | Delay entry until LOB imbalance favorable (IC=+0.116 at 1s on TXFD6) |
| `regime_classifier.py` | `RegimeClassifier` | FAVORABLE/NEUTRAL/ADVERSE from FeatureEngine features |
| `slippage_tracker.py` | `SlippageTracker` | Per-fill slippage measurement |
| `mtm.py` | `MarkToMarket` | Real-time mark-to-market |
| `fill_dlq.py` | `OrphanedFillDLQ` | Orphaned fills (strategy_id="UNKNOWN"), retried via resolver |
| `trigger_executor.py` | `TriggerExecutor` | Conditional trigger execution |

### gateway/ (6 files) — CE-M2, optional via `HFT_GATEWAY_ENABLED=1`
| File | Class | Purpose |
| --- | --- | --- |
| `service.py` | `GatewayService` | 7-step synchronous dispatch |
| `channel.py` | `LocalIntentChannel` | Bounded 4096 + TTL envelope |
| `dedup.py` | `IdempotencyStore` | Exactly-once via TTL cache |
| `exposure.py` | `ExposureStore` | Notional/qty guard, 10K max, zero-balance eviction |
| `policy.py` | `GatewayPolicy` | Session phase, kill-switch, budget |
| `leader_lease.py` | `FileLeaderLease` | File-based HA leader election |

## Data Flow

```text
OrderCommand (from RiskEngine or GatewayService)
  -> order_queue (2048)
  -> OrderAdapter.run()
     [deadline check -> rate limit -> circuit breaker -> coalesce 5ms]
  -> client.place_order() [blocking API]
     Register: live_orders[key], tca_map[key]

Broker callback -> raw_exec_queue (8192)
  -> ExecutionRouter.run()
     -> normalizer.normalize_order() -> OrderEvent -> bus + recorder
     -> normalizer.normalize_fill() -> FillEvent
        [Enrich TCA: decision_price, arrival_price]
        [Measure e2e latency]
        -> PositionStore.on_fill_async()
           Integer arithmetic: PnL = (exit - entry) * qty * multiplier
        -> risk_engine.notify_fill_pnl()
        -> Publish [PositionDelta, FillEvent] -> bus + recorder
```

## Critical Rules

1. **Precision Law**: All prices are `int` scaled x10000. Never float in position/PnL math.
2. **Position closing logic**: `if signs_differ: PnL = (exit - entry) * close_qty * multiplier`
3. **Strategy ID resolution**: custom field -> order_id_map -> "UNKNOWN" -> DLQ
4. **TCA enrichment**: OrderAdapter stores `(decision_price, arrival_price)`, ExecutionRouter enriches FillEvent
5. **Orphaned fills**: strategy_id="UNKNOWN" -> fill_dlq, retried every 100 events
6. **CANCEL/FORCE_FLAT**: Always pass through HALT (exempt from rate limiting and StormGuard block)
7. **order_id_map**: Max 10,000 entries, FIFO eviction on overflow

## Common Fix Patterns

Based on git history (order/adapter.py changed 16 times in last 200 commits):

- **Rate limiter bypass**: CANCEL/FORCE_FLAT must be exempt
- **HALT TOCTOU**: Check StormGuard in `_api_worker`, not just `execute()`
- **DLQ drain**: Must respect TTL, evict expired entries
- **Circuit breaker**: Don't record failures during quarantine
- **TCA maps**: Must be bounded (FIFO eviction) to prevent OOM

## Testing

```bash
make test-file FILE=tests/unit/test_execution_router_loop.py
make test-file FILE=tests/unit/test_order_adapter_safety.py
make test-file FILE=tests/unit/test_position_store.py
```

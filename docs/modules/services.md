# services

## Purpose

Top-level orchestration, service graph wiring, and the MarketDataService pipeline.

## Key Files

- `src/hft_platform/services/system.py`: `HFTSystem` — service supervisor, StormGuard owner, lifecycle hooks.
- `src/hft_platform/services/bootstrap.py`: `SystemBootstrapper` — builds service graph from settings.
- `src/hft_platform/services/registry.py`: Service registry (shared references).
- `src/hft_platform/services/market_data.py`: `MarketDataService` — **966 lines**, most complex service.
- `src/hft_platform/services/execution.py`: Execution pipeline wiring.

## MarketDataService (966 lines)

The core data pipeline. Key behaviors:

### FeedState FSM

`INIT → CONNECTING → SNAPSHOTTING → CONNECTED → DISCONNECTED → RECOVERING`

### Reconnect Logic (3 tiers)

| Tier            | Gap Threshold                         | Action                                 |
| --------------- | ------------------------------------- | -------------------------------------- |
| Resubscribe     | 15s (`HFT_MD_RESUBSCRIBE_GAP_S`)      | Re-subscribe existing contracts        |
| Reconnect       | 60s (`HFT_MD_RECONNECT_GAP_S`)        | Full reconnect if resubscribe fails ×2 |
| Force Reconnect | 300s (`HFT_MD_FORCE_RECONNECT_GAP_S`) | Force new broker session               |

### Per-Symbol Watchdog

- Runs every `HFT_WATCHDOG_INTERVAL_S` (default 1s).
- If ≥2 symbols exceed `HFT_SYMBOL_GAP_THRESHOLD_S` (default 5s), triggers resubscribe.
- Relaxed threshold during market open grace period (`HFT_MARKET_OPEN_GRACE_S`).

### Recording Integration

- Direct recording via `_record_direct_event()` with drop-on-full policy.
- Graceful degradation: enters degraded mode after `HFT_RECORD_DEGRADE_THRESHOLD` (500) drops.

### Threading

- Shioaji callbacks run in a **broker thread**. Uses `loop.call_soon_threadsafe()` to enqueue.
- Per-symbol tick timestamp updated inline (P0-2: CPython dict assignment is GIL-atomic).

## SystemBootstrapper

Creates and connects all services based on settings:

1. Creates queues (raw_queue, recorder_queue, risk_queue).
2. Instantiates MarketDataService, StrategyRunner, RiskEngine, OrderAdapter, RecorderService.
3. If `HFT_GATEWAY_ENABLED=1`: creates GatewayService with LocalIntentChannel.
4. Wires execution router for fill/order events.

## Extension Points

- Add new services and register them in `bootstrap.py`.
- Expand system lifecycle hooks in `system.py` (start/stop/health).

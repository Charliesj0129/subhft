

# FeedAdapter Module – Spec

## Problem Statement
Provide a low-latency, resilient connector between Shioaji’s WS/REST APIs (per `sinotrade_tutor_md`) and our in-process event bus. The FeedAdapter must honor Sinotrade’s usage constraints (≤200 subscriptions, snapshot batch ≤500, rate limits in `limit.md`), handle authentication, heartbeat monitoring, reconnection, timestamp capture, and deliver normalized payloads into the LOB engine without blocking broker callback threads.

## Requirements
### Functional
1. **Session Management**
   - Support login with credentials/env vars (per `login.md`) and optional CA activation.
   - Enforce `limit.md` constraints: ≤5 concurrent connections per person_id, ≤200 `api.subscribe` calls, ≤500 contracts per snapshot request.
2. **Callback Discipline**
   - Shioaji callbacks (Tick/BidAsk) must perform only timestamp capture + enqueue into an asyncio queue. All heavy decoding happens on our pinned consumer thread.
3. **Normalization Pipeline**
   - Consumer task reads raw payloads, classifies (Tick/BidAsk), normalizes via `MarketDataNormalizer`, and feeds `LOBEngine` + event bus.
   - Capture both `exchange_ts` and `local_ts` (via `rdtsc`/`time_ns`) on each event.
4. **Heartbeat & Reconnect**
   - Monitor last event time; if gap > threshold, trigger graceful reconnect: set state `DISCONNECTED`, clear queues, fetch snapshots, resubscribe.
   - Support manual restart via CLI hook.
5. **Snapshot Bootstrap**
   - Use `api.snapshots` per `market_data/snapshot.md` (≤500 contracts, 50 requests/5s). Apply normalized snapshots before enabling strategies.
6. **Timer Integration**
   - Provide optional timer ticks (10–100 ms) published onto bus for scheduling and heartbeat checks.

### Non-Functional
1. **Latency**: <50 µs callback overhead; <20 µs p99 from callback to bus publish under nominal load.
2. **Reliability**: Automatic reconnect within 2 s; configurable retry/backoff; log + alert on repeated failures.
3. **Observability**: Structured logs for lifecycle events, metrics for feed events/sec, callback lag, reconnect count, subscription usage.
4. **Configuration**: YAML-driven symbol lists, subscription options, heartbeat thresholds; hot-reload support preferred.

### Interfaces
- `ShioajiClient`: wraps Shioaji API (`sinotrade_tutor_md`).
- `FeedAdapter`: manages queues, consumer tasks, state machine.
- Outputs normalized events to `RingBufferBus`.

## Deliverables
- Updated `feed_adapter/shioaji_client.py` + `feed_adapter/adapter.py` (if separate).
- Tests: unit (normalization, queue discipline) and integration (simulated feed, reconnect scenario).
- Docs: usage guide (login, config), troubleshooting, metrics references.

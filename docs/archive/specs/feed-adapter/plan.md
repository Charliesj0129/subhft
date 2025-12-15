# Plan – FeedAdapter

## Components
1. **ShioajiClient Wrapper**
   - Encapsulate login (`sinotrade_tutor_md/login.md`), snapshot fetch (`market_data/snapshot.md`), subscription management/lists (`limit.md`), heartbeat stats.
   - Provide async-safe callback registration that pushes into internal queue.

2. **FeedAdapter Runtime**
   - `raw_queue`: `asyncio.Queue` bounded; callback enqueues, consumer dequeues.
   - Consumer coroutine pinned to specific event loop thread core (optional) to minimize jitter.
   - State machine (`INIT → SNAPSHOTTING → CONNECTED → DISCONNECTED → RECOVERING`).

3. **Heartbeat & Reconnect**
   - Track last event timestamp; schedule monitor task (≥10 ms).
   - On gap > threshold or explicit error, cancel subscriptions, mark DISCONNECTED, fetch snapshots, resubscribe.

4. **Snapshot Bootstrap**
   - Load symbol config; batch snapshot requests per `market_data/snapshot.md` (≤500 contracts, ≤50 req / 5s) with throttling.
   - Normalize snapshot payloads to feed LOB engine before enabling strategies.

5. **Timer Tick Generator**
   - `asyncio` task emits timer events at configured interval to event bus (shared or separate component).

6. **Configuration & CLI**
   - YAML config: credentials, symbol list, heartbeat thresholds, timer settings.
   - CLI commands for reload, reconnect, stats (e.g., `python -m hft_platform.feed_adapter.cli status`).

7. **Observability Hooks**
   - Integrate `MetricsRegistry`: counters for events, gauge for callbacks lag, reconnect count, subscription usage.
   - Structured logs via `structlog`: lifecycle events, errors, reconnect attempts.

## Implementation Steps
1. Refactor `shioaji_client.py` to expose async-friendly login/subscribe, snapshots, usage polling.
2. Implement `FeedAdapter` class managing queues, consumer task, heartbeat monitor, timer.
3. Update `main.py` to instantiate `FeedAdapter`, start tasks, and handle shutdown.
4. Add tests (unit + integration) using mock Shioaji responses.
5. Document configuration, CLI usage, reconnect scenarios.

# Tasks – FeedAdapter

| ID | Title | Description & Acceptance | Dependencies |
| --- | --- | --- | --- |
| F1 | Refine Shioaji client wrapper | Implement login (per `sinotrade_tutor_md/login.md`), snapshot batching (≤500, `market_data/snapshot.md`), subscription enforcement (≤200 symbols, `limit.md`), and callback registration that pushes into bounded queue. **Acceptance**: unit tests simulate login/snapshot; warning logged when symbol list exceeds limit. | — |
| F2 | Implement FeedAdapter state machine | Create FeedAdapter class managing raw queue, consumer task, and states (`INIT/CONNECTED/DISCONNECTED`). Includes structured logging and metrics counters. **Acceptance**: integration test transitions through states on simulated disconnect. | F1 |
| F3 | Callback discipline & normalization pipeline | Ensure callbacks capture timestamps and enqueue only; consumer normalizes events via `MarketDataNormalizer`, updates LOB, publishes to bus. **Acceptance**: benchmark shows <50 µs callback overhead; normalized events reach bus. | F2 |
| F4 | Heartbeat monitor & reconnect | Add monitor task detecting gaps > threshold, triggering graceful reconnect (snapshot fetch, resubscribe). **Acceptance**: simulated silence triggers reconnect within 2 s; logs/metrics record event. | F3 |
| F5 | Timer tick generator | Implement timer task publishing `TimerTick` events at configured interval. **Acceptance**: strategies observe timer events; metrics track tick rate. | F2 |
| F6 | Configuration & CLI tooling | Define YAML config for credentials/symbols/heartbeat; add CLI commands for status/reconnect/reload. **Acceptance**: CLI displays current subscriptions and allows manual reconnect. | F2 |
| F7 | Observability integration | Wire MetricsRegistry counters/gauges (events/sec, callback lag, reconnect count) and structured logs. Add docs describing metrics. **Acceptance**: `/metrics` shows feed metrics; logs include lifecycle events. | F2, F4 |
| F8 | Testing & docs | Add unit/integration tests (mock Shioaji) and write `docs/feed_adapter.md` covering setup, troubleshooting, and metrics. **Acceptance**: tests pass; doc reviewed. | F1–F7 |

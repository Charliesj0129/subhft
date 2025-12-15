# Tasks – LOB Engine

| ID | Title | Description & Acceptance | Dependencies |
| --- | --- | --- | --- |
| L1 | Design BookState structure | Implement per-symbol `BookState` with top-5 arrays, metadata, derived feature fields. **Acceptance**: unit test instantiates state, fields default correctly. | — |
| L2 | Snapshot handler | Apply normalized snapshots (from `market_data/snapshot.md`) to BookState, reset ladders, increment version, emit snapshot event. **Acceptance**: test applies snapshot and verifies top levels + version. | L1 |
| L3 | BidAsk incremental updates | Process `BidAsk` events to update levels, recompute derived stats, validate ordering, log anomalies. **Acceptance**: test updates differ per level and derived metrics (mid/spread) correct. | L1 |
| L4 | Tick handling & trade metrics | Update last trade info from Tick events, compute last aggressor and integrate with features. **Acceptance**: tick updates stored in BookState; imbalance updates referencing tick info as needed. | L1 |
| L5 | Feature computation & API | Compute mid/spread/imbalance/depth totals/queue deltas; expose `get_features(symbol)` and optional `MarketFeature` event emission. **Acceptance**: strategies can fetch features; bus receives feature events. | L2–L4 |
| L6 | Concurrency & strategy integration | Provide thread-safe read access (copy/RCU) and integrate with StrategyRunner context injection. **Acceptance**: StrategyContext contains LOB/feature references in end-to-end test. | L5 |
| L7 | Observability | Add metrics/logs for updates, snapshots, degraded symbols; document usage. **Acceptance**: metrics visible in `/metrics`; logs appear on anomalies. | L2–L5 |
| L8 | Testing & documentation | Write unit tests for snapshot/incremental/tick scenarios, plus doc describing BookState schema & features. **Acceptance**: tests pass; doc reviewed. | L1–L7 |

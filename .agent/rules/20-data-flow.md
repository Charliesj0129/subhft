# Data Flow

Runtime path: broker callback -> `call_soon_threadsafe` -> bounded raw queue -> normalizer -> LOB -> FeatureEngine -> RingBufferBus -> StrategyRunner -> `OrderIntent` -> Gateway/Risk -> `OrderCommand` -> OrderAdapter -> broker -> execution -> positions/reconciliation.

Recording path: market event -> recorder queue `put_nowait()` -> RecorderService -> Batcher -> ClickHouse, with WAL fallback/wal-first as configured.

Invariants:

- Recording never blocks hot path; bounded queue overflow has explicit drop/degrade policy.
- Normalizer emits scaled int prices x10000; raw floats do not pass downstream.
- Use `timebase.now_ns()` for local timestamps; preserve exchange/source timestamp.
- Broker callbacks cross threads only via thread-safe event-loop handoff.
- Verify changed flows with queue depth, latency histograms, metrics, and WAL/ClickHouse ingestion.

Full trace: `docs/architecture/pipeline-chains.md`.

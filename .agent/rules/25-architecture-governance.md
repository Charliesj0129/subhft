# Architecture Governance

Dependency direction:

- `feed_adapter` emits events/contracts; broker SDK imports stay inside broker packages.
- `strategy`, `risk`, `order`, `execution` communicate through contracts.
- `contracts` and `events.py` never import runtime services, strategies, execution, or recorder internals.
- `services` orchestrates; domain logic stays in domain packages.

Runtime changes must declare where they enter the canonical flow in `20-data-flow.md`. New event-loop stages use bounded queues and explicit overflow policy. HALT blocks new orders; cancels remain allowed. Crashes on execution/order/risk paths surface to supervisor and metrics.

Persistence: ClickHouse failure falls back to WAL; replay is idempotent/dedup-aware. ClickHouse schema source is `src/hft_platform/migrations/clickhouse/`.

Rust/Python: deterministic CPU-heavy hot transforms prefer Rust; copy-heavy FFI is rejected unless justified and measured. Rust fallback must be explicit and observable.

Alpha: research artifacts do not directly enable live. Promotion is gated, config-driven, reversible, and latency-realistic. `src/hft_platform/alpha/` and `research/` may use float for offline metrics only; live accounting paths may not.

Exposure maps must declare max cardinality and eviction. Default exposure cap: 10,000 entries; evict zero-balance first, otherwise reject with `ExposureLimitError` and warning log.

Architecture-affecting changes update relevant docs/codemaps and boundary tests.

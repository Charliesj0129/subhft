# Architecture Governance Rules

This rule defines architecture guardrails for changes across the HFT Platform.

## 1. Dependency Direction

Allowed direction (high level):

1. `feed_adapter` -> `events`
2. `strategy` -> `contracts.strategy`
3. `risk` -> `contracts.strategy`
4. `order` -> `contracts.strategy`
5. `execution` -> `contracts.execution`
6. `recorder` -> mapped records only (no strategy/risk logic)
7. `services` orchestrates modules but does not own domain logic

Disallowed direction:

1. `contracts` importing runtime services
2. `events` importing strategy or execution modules
3. recorder writing directly into strategy/risk internals

## 2. Canonical Runtime Dataflow

Required order for live runtime:

1. market callback -> `raw_queue`
2. normalize + LOB -> bus events
3. strategy -> `OrderIntent`
4. risk -> `OrderCommand`
5. order adapter -> broker API
6. execution callback -> normalized execution events
7. positions/reconciliation updates
8. recorder persistence (ClickHouse/WAL)

Any new feature must explicitly choose where it enters this flow.

## 3. Queue and Bus Contracts

1. New hot-path stage must use bounded queues.
2. `QueueFull` behavior must be explicit: drop, degrade, or block.
3. Event bus overflow handling must remain safety-aware and HALT-capable.
4. Do not add unbounded buffers in event-loop paths.

## 4. Precision and Time Rules

1. Monetary values in risk/order/execution/position paths use scaled int or Decimal.
2. Timestamp semantics:
- exchange/source timestamp is preserved
- local ingest timestamp is captured at stage boundary
3. Any fallback float usage must be marked as non-accounting only.

## 5. Recorder Durability Rules

1. ClickHouse failure must not drop data silently.
2. WAL fallback is mandatory for recorder write failures.
3. WAL replay must remain idempotent-safe or dedup-aware.
4. Schema changes must preserve replay compatibility for existing WAL files.
5. Runtime schema source of truth is `src/hft_platform/schemas/clickhouse.sql` only.
6. Legacy SQL files are non-bootstrap references unless explicitly migrated.

## 6. StormGuard and Safety

1. HALT state must block new order progression.
2. Cancel actions remain allowed in HALT.
3. Any component crash in execution path must be surfaced to supervisor and metrics.

## 7. Change Protocol (for all architecture-affecting PRs)

Required updates when changing flow, contracts, or persistence:

1. update `docs/modules/<module>.md`
2. update `.agent/library/current-architecture.md` if boundaries or responsibilities changed
3. add/adjust tests for behavior at the affected boundary
4. validate metrics and recorder ingestion after change
5. for any new component that touches the hot path: create a 5-gate design review entry in `.agent/library/design-review-artifacts.md`

## 8. Rust/Python Boundary

1. Prefer Rust for CPU-heavy deterministic transforms in hot path.
2. Python<->Rust interfaces should avoid unnecessary copies.
3. Fallback behavior (when Rust module unavailable) must be explicit and observable.

## 9. Broker Adapter Decomposition

1. `ShioajiClient` logic should be separated by concern:
- session lifecycle
- contract resolution
- quote stream/callbacks
- order gateway
- account/usage queries
2. Quote path failures must not directly impair order path availability.
3. New broker SDK changes should land in the smallest affected subcomponent.

## 10. Alpha Contribution Governance

1. Production alpha changes must flow through contribution gates:
- feasibility
- correctness
- backtest
- portfolio integration
- production readiness
2. Promotion must be config-driven and reversible (canary + rollback).
3. No direct production enable from research-only artifacts.

## 11. Alpha Module Float Exception

1. `float` is permitted in `src/hft_platform/alpha/` and `research/` modules.
2. These modules are offline-only (CLI-invoked research pipeline) and not financial accounting code.
3. Sharpe ratio, drawdown, IC, and other scorecard metrics may use `float`.
4. The Precision Law (Rule 4) applies exclusively to live trading paths: `risk/`, `order/`, `execution/`, `position`, and any hot-path accounting arithmetic.
5. Mixed-use modules that touch both offline and live paths must use scaled int or Decimal for all accounting values.

## 12. ExposureStore Cardinality Bound

1. Any exposure tracking dict keyed by (account, strategy, symbol) or any similar triple MUST declare a maximum cardinality and an eviction policy.
2. Default maximum is 10,000 entries (env `HFT_EXPOSURE_MAX_SYMBOLS`).
3. Eviction policy: zero-balance entries are evicted first on overflow; if still over limit, new symbol registration MUST be rejected with an explicit error (`ExposureLimitError`).
4. The eviction must be logged at `WARNING` level with the account, strategy, and symbol context.
5. Unbounded exposure maps are a critical OOM risk in production; this rule applies to all future exposure-tracking components (tracked as CE2-12).

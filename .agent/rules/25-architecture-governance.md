# Architecture Governance Rules

Architecture guardrails for changes across the HFT Platform.

## 1. Dependency Direction

Allowed:
1. `feed_adapter` -> `events`
2. `strategy` / `risk` / `order` -> `contracts.strategy`
3. `execution` -> `contracts.execution`
4. `recorder` -> mapped records only (no strategy/risk logic)
5. `services` orchestrates; does not own domain logic

Disallowed:
1. `contracts` importing runtime services
2. `events` importing strategy or execution modules
3. recorder writing directly into strategy/risk internals

## 2. Canonical Runtime Dataflow

Required ordering: market callback -> `raw_queue` -> normalize + LOB -> bus events -> strategy (`OrderIntent`) -> risk (`OrderCommand`) -> order adapter -> broker API -> execution callback -> normalized execution events -> positions/reconciliation -> recorder (ClickHouse/WAL).

Any new feature MUST explicitly choose where it enters this flow. (See `20-data-flow.md` for the full chain.)

## 3. Queue and Bus Contracts

1. New hot-path stages MUST use bounded queues.
2. `QueueFull` behavior MUST be explicit: drop, degrade, or block.
3. Event bus overflow MUST remain safety-aware and HALT-capable.
4. No unbounded buffers in event-loop paths.

## 4. Precision and Time

1. Monetary values in risk/order/execution/position use scaled int or Decimal (see `01-core-laws.md` Law 4).
2. Exchange/source timestamp preserved; local ingest timestamp captured at each stage boundary.
3. Fallback float usage MUST be marked non-accounting only.

## 5. Recorder Durability

1. ClickHouse failure MUST NOT drop data silently; WAL fallback is mandatory.
2. WAL replay MUST be idempotent-safe or dedup-aware.
3. Schema changes MUST preserve replay compatibility.
4. Runtime schema source of truth: `src/hft_platform/migrations/clickhouse/` (sequential). Legacy SQL files are non-bootstrap references.

## 6. StormGuard and Safety

1. HALT MUST block new order progression. Cancel remains allowed in HALT.
2. Any execution-path crash MUST surface to supervisor and metrics.

## 7. Change Protocol (architecture-affecting PRs)

When flow, contracts, or persistence change:
1. Update `docs/modules/<module>.md`.
2. Update `.agent/library/current-architecture.md` if boundaries changed.
3. Add/adjust tests at the affected boundary.
4. Validate metrics and recorder ingestion post-change.
5. New hot-path components: add a 5-gate design review entry in `.agent/library/design-review-artifacts.md`.

## 8. Rust/Python Boundary

1. Prefer Rust for CPU-heavy deterministic transforms in hot path.
2. Avoid unnecessary copies across the boundary (see `01-core-laws.md` Law 5).
3. Fallback when Rust module unavailable MUST be explicit and observable.

## 9. Broker Adapter Decomposition

Registry in `feed_adapter/broker_registry.py`; `HFT_BROKER` selects active broker (default `shioaji`). Each broker is self-contained under `feed_adapter/<broker>/` with: `facade.py`, `session_runtime.py`, `quote_runtime.py`, `order_gateway.py`, `account_gateway.py`, `contracts_runtime.py`. Quote path failures must not impair order path.

Detailed rules (MB-01..MB-10): see `26-multi-broker-governance.md`.

## 10. Alpha Contribution Governance

1. Production alpha changes flow through gates: feasibility → correctness → backtest → portfolio integration → production readiness.
2. Promotion MUST be config-driven and reversible (canary + rollback).
3. No direct production enable from research-only artifacts.

## 11. Alpha Module Float Exception

1. `float` is permitted in `src/hft_platform/alpha/` and `research/` (offline-only CLI pipeline); Sharpe/drawdown/IC metrics may use `float`.
2. Precision Law applies exclusively to live paths: `risk/`, `order/`, `execution/`, `position`, and any hot-path accounting arithmetic.
3. Mixed-use modules touching both paths MUST use scaled int or Decimal for all accounting values.

## 12. ExposureStore Cardinality Bound (CE2-12)

1. Any exposure map keyed by (account, strategy, symbol) or similar MUST declare max cardinality and eviction policy.
2. Default max: 10,000 entries (env `HFT_EXPOSURE_MAX_SYMBOLS`).
3. Eviction: zero-balance first; if still over, reject with `ExposureLimitError`.
4. Eviction MUST log at `WARNING` with account/strategy/symbol context.

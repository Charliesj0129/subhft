# HFT Platform Target Architecture (Decision Set V2)

Date: 2026-02-21
Status: Active execution plan (aligned to current codebase)
Companion cluster backlog: `.agent/library/cluster-evolution-backlog.md`.

## A. Decision Ledger and Current Status

| Decision | Status | Current evidence (as-built) | Remaining work |
|---|---|---|---|
| D1: Canonical runtime schema | In Progress | Recorder writer/loader bootstrap from `src/hft_platform/schemas/clickhouse.sql` via `recorder/schema.py` | Remove ambiguity from legacy SQL files and enforce CI checks against non-canonical runtime bootstrap paths |
| D2: Shioaji adapter decomposition | Pending | `src/hft_platform/feed_adapter/shioaji_client.py` still centralizes session/quote/order/account concerns | Split into session/contracts/quote/order/account modules with facade and staged migration |
| D3: Alpha contribution pipeline (Gate A-E) | Mostly Implemented | `hft alpha validate/promote/canary/experiments/pool/rl-promote` and `src/hft_platform/alpha/*` are live | Integrate runtime canary metrics feed and add stronger operator workflows/automation |
| D4: Research artifact standardization | Implemented | `research/registry`, `research/experiments`, scaffold templates, scorecard and meta artifacts are wired | Add retention policy and artifact GC tooling |
| D5: Audit logging for alpha lifecycle | In Progress | Schema (`audit.sql`) and client (`alpha/audit.py`) implemented; bootstrap command missing from deploy/init path | Ensure audit schema bootstrap is part of deploy/init path (not manual) — tracked as M1 gap |
| D6: Backtest consistency and reliability | In Progress | Runtime backtest equity extraction (`backtest/equity.py`) added; research backtest standardized | Define contract boundaries between runtime and research backtest paths; smoke/research split policy still partial |
| D-CE2: Order/Risk Gateway (CE-M2) | Implemented | `src/hft_platform/gateway/` — channel, dedup, exposure, policy, service all live; enabled via `HFT_GATEWAY_ENABLED=1` | Hardening: CE2-07 (metrics/SLOs), CE2-08 (chaos tests), CE2-09 (HA/leader lease), CE2-11 (quote schema v1 lock), CE2-12 (ExposureStore memory bound ✅ done) |
| D-CE3: Async WAL-First (CE-M3) | Implemented | `src/hft_platform/recorder/wal_first.py`, `disk_monitor.py`, `mode.py`, `shard_claim.py`, `replay_contract.py` all live; enabled via `HFT_RECORDER_MODE=wal_first` | Hardening: CE3-03 (scale-out loaders), CE3-04 (replay safety contract tests), CE3-06 (WAL SLO metrics), CE3-07 (outage drills) |

## B. Target State (Next Stable Architecture)

1. Runtime trading core remains low-latency and bounded
- Event flow remains queue/bus isolated with strict HALT behavior.
- Hot path keeps Rust acceleration where measurable.

2. Broker integration is modular by concern
- `session`, `contracts`, `quote_stream`, `order_gateway`, `account` submodules under `feed_adapter/shioaji/`.
- Facade compatibility for incremental cutover.

3. Alpha lifecycle is deterministic and auditable end-to-end
- Gate A-E outputs are reproducible artifacts.
- Experiment runs and promotion decisions are queryable with stable schemas.
- Canary actions map directly to config changes and audit rows.

4. Schema governance is explicit
- One canonical runtime DDL.
- One explicit audit DDL bootstrap path.
- Migration and replay compatibility checks are required for all schema changes.

5. Backtest domains are cleanly separated
- Runtime backtest supports strategy smoke and execution-shape validation.
- Research backtest is the canonical promotion scoring engine.

## C. Milestones (Rebased)

### M1: Schema and Audit Hardening — Partial
Status: `clickhouse.sql` is canonical; `audit.sql` exists. Auto-bootstrap command missing.
1. Add an explicit deploy/bootstrap command to apply both runtime and audit schema sets.
2. Add CI rule: runtime code cannot bootstrap from deprecated SQL files.
3. Add replay compatibility check for schema PRs.

### M2: Shioaji Decomposition — Pending
Status: `ShioajiClient` still monolithic. No decomposition started.
1. Introduce submodules and facade with no behavior change.
2. Move quote path first (highest operational risk).
3. Move order/account paths next.
4. Add per-submodule health metrics.

### M3: Alpha Ops Integration — Implemented
Status: All 6 pipeline stages live (validation, promotion, canary, pool, experiments, audit). See `research_pipeline_execution_plan.md`.
Remaining: stronger operator automation, runtime metrics feed into canary evaluation.
1. Wire canary evaluation to runtime metrics snapshots and scheduler.
2. Add promotion rollback playbook and operator command wrappers.
3. Add retention policy for `research/experiments` and stale promotion configs.

### M4: Backtest Governance — In Progress
Status: `equity.py` added for real-equity-first extraction. Research/runtime backtest split policy not yet documented.
1. Publish boundary doc: runtime backtest vs research backtest responsibilities.
2. Gate production promotion on research backtest artifacts only.
3. Keep runtime backtest as smoke/stability path with clear constraints.

## D. Non-Negotiable Constraints

1. No blocking I/O on runtime hot path event loops.
2. Monetary-critical logic must use scaled-int or Decimal semantics.
3. Queue/bus boundaries remain bounded and overflow behavior explicit.
4. HALT must stop new order progression while allowing safe cancel behavior.
5. Promotion to production must remain gate-driven and reversible.

## E. Cluster Evolution Vectors (Implemented — Hardening In Progress)

Status: CE-M2 and CE-M3 core modules implemented 2026-02-21. Hardening backlog open.
Detailed milestone/issue backlog: `.agent/library/cluster-evolution-backlog.md`.
Design review artifacts: `.agent/library/design-review-artifacts.md`.
C4 diagrams: `.agent/library/c4-model-current.md`.

---

### Vector 2 - Dedicated Order/Risk Gateway (CE-M2) — Implemented

**Status**: Core modules live (2026-02-21). Enable with `HFT_GATEWAY_ENABLED=1`.
**Design review artifacts**: `.agent/library/design-review-artifacts.md` § CE-M2

**Implemented**:
- CE2-01: `OrderIntent` wire schema with `idempotency_key`, `ttl`, `trace_id` (`contracts/strategy.py`)
- CE2-02: `LocalIntentChannel` — bounded asyncio queue, ack/nack, DLQ (`gateway/channel.py`)
- CE2-03: `GatewayService` — asyncio dispatch loop with 7-step pipeline (`gateway/service.py`)
- CE2-04: `ExposureStore` — atomic CAS, per-symbol/strategy/account, memory-bounded (CE2-12) (`gateway/exposure.py`)
- CE2-05: `IdempotencyStore` — fixed-capacity dedup window with persist/load (`gateway/dedup.py`)
- CE2-06: `GatewayPolicy` FSM — NORMAL/DEGRADED/HALT with CANCEL pass-through (`gateway/policy.py`)
- CE2-10: `LocalIntentChannel` callback path is enqueue-only (design enforced by async boundary)
- CE2-12: `ExposureStore` memory bound — `_max_symbols` + zero-balance eviction + `ExposureLimitError`

**Hardening Backlog (CE-M2 TODO)**:
- [ ] CE2-07: Metrics/SLOs — `gateway_dispatch_latency_ns`, `gateway_reject_total`, `gateway_dedup_hits_total` dashboards
- [ ] CE2-08: Chaos test — multi-runner + gateway outage, verify no duplicate broker dispatch
- [ ] CE2-09: Active/standby gateway HA with leader lease; only leader dispatches to broker
- [ ] CE2-11: `quote_version=v1` schema guard with reject-and-alert on mismatch

---

### Vector 3 - Asynchronous WAL Separation (CE-M3) — Implemented

**Status**: Core modules live (2026-02-21). Enable with `HFT_RECORDER_MODE=wal_first`.
**Design review artifacts**: `.agent/library/design-review-artifacts.md` § CE-M3

**Implemented**:
- CE3-01: `RecorderMode` enum + env flag `HFT_RECORDER_MODE=direct|wal_first` (`recorder/mode.py`)
- CE3-02: `WALFirstWriter` — no ClickHouse calls; disk pressure → per-topic policy (`recorder/wal_first.py`)
- CE3-05: `DiskPressureMonitor` — background daemon, OK/WARN/CRITICAL/HALT levels + hooks (`recorder/disk_monitor.py`)
- Shard claim: `FileClaimRegistry` — fcntl-based exclusive file ownership (`recorder/shard_claim.py`)
- Replay contract: `ReplayContract` type definitions (`recorder/replay_contract.py`)

**Hardening Backlog (CE-M3 TODO)**:
- [ ] CE3-03: Scale-out WAL loader workers with shard-claim protocol integration tests
- [ ] CE3-04: Full replay safety contract (ordering + dedup + manifest) tested under restart/crash scenarios
- [ ] CE3-06: WAL SLO metrics — backlog size, replay lag, replay throughput, drain ETA
- [ ] CE3-07: Outage drills: ClickHouse down, slow, WAL disk-full, loader restart + recovery runbook

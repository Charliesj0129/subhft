# Cluster Evolution Backlog (Milestone / Issue)

Date: 2026-02-21
Scope: Vector 2 (Order/Risk Gateway) + Vector 3 (Async WAL Separation)
Status: CE-M2 and CE-M3 core modules implemented. Hardening backlog items remain.

## 1. Planning Rules

1. Every issue below is directly assignable and can be created as a ticket 1:1.
2. `Owner` defaults to `TBD` and should be set before sprint planning.
3. `Estimate` is engineering effort (single engineer, implementation + tests + docs).
4. `Done` means code merged, tests passing, observability updated, and runbook updated.

## 2. Milestone CE-M2 - Dedicated Order/Risk Gateway (Vector 2)

Target window: 4-6 weeks  
Goal: isolate risk + order dispatch into a cluster-level gateway with global exposure control.

Exit criteria:
1. All strategy workers submit intents through distributed channel only.
2. Global exposure checks are enforced in one gateway path.
3. Duplicate command submits cannot create duplicate broker orders.
4. Gateway failure mode is explicit (`reject/degrade/halt`) and observable.
5. Gateway active/standby failover is deterministic and bounded.
6. Quote callback path is enqueue-only and quote schema is locked to v1.

### 2.1 Issue List

| ID | Title | Priority | Estimate | Dependencies | Owner | Status |
|---|---|---|---|---|---|---|
| CE2-01 | Define gateway command envelope and idempotency contract | P0 | 2d | None | TBD | ✅ Implemented |
| CE2-02 | Implement distributed intent channel adapter (ack/retry semantics) | P0 | 4d | CE2-01 | TBD | ✅ Implemented |
| CE2-03 | Create gateway service skeleton (`RiskEngine` + `OrderAdapter`) | P0 | 4d | CE2-01, CE2-02 | TBD | ✅ Implemented |
| CE2-04 | Add global exposure state store and atomic check/update path | P0 | 4d | CE2-03 | TBD | ✅ Implemented |
| CE2-05 | Implement command dedup and replay-safe processing | P0 | 3d | CE2-01, CE2-03 | TBD | ✅ Implemented |
| CE2-06 | Add gateway fail-safe policy (reject/degrade/halt) and config flags | P1 | 2d | CE2-03 | TBD | ✅ Implemented |
| CE2-10 | Isolate Shioaji callbacks to enqueue-only fast path | P0 | 3d | CE2-03 | TBD | ✅ Implemented |
| CE2-12 | ExposureStore memory bound (symbol cardinality limit + eviction) | P0 | 1d | CE2-04 | TBD | ✅ Implemented |
| CE2-07 | Add gateway metrics/alerts/dashboard and SLO definitions | P1 | 2d | CE2-03, CE2-06 | TBD | 🔄 TODO |
| CE2-09 | Add active/standby gateway failover and leader lease control | P0 | 4d | CE2-03, CE2-05, CE2-06 | TBD | 🔄 TODO |
| CE2-11 | Enforce quote schema lock (`quote_version=v1`) with guardrails | P0 | 2d | CE2-10 | TBD | 🔄 TODO |
| CE2-08 | Multi-runner integration test and chaos test for gateway outages | P0 | 3d | CE2-04, CE2-05, CE2-06, CE2-09, CE2-10, CE2-11 | TBD | 🔄 TODO |

### 2.2 Issue Specs

#### CE2-01 - Define gateway command envelope and idempotency contract
- Scope:
  - Define wire schema for `OrderIntent` submit and risk decision output.
  - Include `trace_id`, `strategy_id`, `intent_id`, `source_ts_ns`, `idempotency_key`, `ttl`.
- Deliverables:
  - Contract document and typed schema module.
  - Serialization tests.
- Acceptance criteria:
  - Schema is versioned and backward-compatible policy is documented.
  - Duplicate payloads produce same `idempotency_key`.

#### CE2-02 - Implement distributed intent channel adapter (ack/retry semantics)
- Scope:
  - Build transport abstraction for strategy -> gateway command submission.
  - Support ack, timeout, retry, and dead-letter behavior.
- Deliverables:
  - Channel adapter module and failure simulation tests.
- Acceptance criteria:
  - Lost ack and timeout scenarios are covered by tests.
  - No unbounded queue behavior in hot path.

#### CE2-03 - Create gateway service skeleton (`RiskEngine` + `OrderAdapter`)
- Scope:
  - Extract runtime gateway process that owns risk/order dispatch.
  - Add service entrypoint and lifecycle management.
- Deliverables:
  - Gateway service module, startup config, health endpoint.
- Acceptance criteria:
  - Strategy workers can submit intents to gateway in integration test.
  - Gateway dispatches to broker adapter through existing order path contracts.

#### CE2-04 - Add global exposure state store and atomic check/update path
- Scope:
  - Centralize symbol/strategy/account exposure checks.
  - Ensure check+update is atomic for concurrent strategy submissions.
- Deliverables:
  - Exposure state component, stress tests for concurrent intents.
- Acceptance criteria:
  - Concurrent load tests show no exposure overshoot.
  - Guardrail breach produces deterministic rejection reason.

#### CE2-05 - Implement command dedup and replay-safe processing
- Scope:
  - Persist recent `idempotency_key` window and dedup terminal decisions.
  - Ensure retried intents do not double-send broker orders.
- Deliverables:
  - Dedup storage module and replay tests.
- Acceptance criteria:
  - Retried command returns previous decision without re-dispatch.
  - Restart scenario preserves dedup guarantees within configured window.

#### CE2-06 - Add gateway fail-safe policy (reject/degrade/halt) and config flags
- Scope:
  - Define behavior when gateway loses broker/session/channel dependencies.
  - Integrate with StormGuard/HALT semantics.
- Deliverables:
  - Policy config, decision matrix doc, runtime hooks.
- Acceptance criteria:
  - Failure drills map to expected policy behavior.
  - Policy state exported via metrics/log fields.

#### CE2-07 - Add gateway metrics/alerts/dashboard and SLO definitions
- Scope:
  - Add queue lag, dedup hits, reject rate, dispatch latency, error rate metrics.
  - Add alerts and dashboard panels.
- Deliverables:
  - Metrics code, Prometheus rules, dashboard JSON update.
- Acceptance criteria:
  - SLOs are measurable and alert thresholds documented.

#### CE2-09 - Add active/standby gateway failover and leader lease control
- Scope:
  - Introduce active/standby gateway processes with a leader lease.
  - Ensure only leader path can dispatch broker orders.
  - Sync or reconstruct required risk/dedup state on failover.
- Deliverables:
  - HA controller module, leader lease adapter, failover runbook.
  - Failover integration tests (leader crash, network partition simulation).
- Acceptance criteria:
  - Leader failover occurs within configured bound (target < 3s).
  - No duplicate broker order dispatch across failover boundary.
  - Active/standby role and lease health are exported in metrics.

#### CE2-10 - Isolate Shioaji callbacks to enqueue-only fast path
- Scope:
  - Move parsing/normalization out of callback threads into worker pool.
  - Keep callback handlers limited to enqueue and minimal metadata tagging.
  - Enforce bounded callback queue with explicit overflow behavior.
- Deliverables:
  - Callback ingress module and worker consumers.
  - Latency instrumentation and overflow policy config.
- Acceptance criteria:
  - Callback handler P99 latency meets budget (target < 100 µs).
  - No heavy parsing logic remains in callback thread code path.
  - Overflow behavior is deterministic and alertable.

#### CE2-11 - Enforce quote schema lock (`quote_version=v1`) with guardrails
- Scope:
  - Pin quote schema to v1 typed payload on startup and runtime.
  - Add schema guard and reject/alert path for mismatched payload versions.
  - Document fallback/override policy for emergency operations only.
- Deliverables:
  - Schema guard module, startup validation, compatibility tests.
  - Operator runbook updates for schema mismatch incidents.
- Acceptance criteria:
  - Runtime starts in strict v1 mode by default.
  - Mismatched schema triggers explicit reject + metric + structured log.
  - Integration tests confirm typed payload path end-to-end.

#### CE2-08 - Multi-runner integration test and chaos test for gateway outages
- Scope:
  - Build test harness for N strategy workers + single gateway.
  - Add outage injection for channel/gateway process.
- Deliverables:
  - Integration and chaos tests in CI-compatible form.
- Acceptance criteria:
  - Tests prove no duplicate order dispatch under retry/outage conditions.
  - HALT path is verified for severe gateway failures.

## 3. Milestone CE-M3 - Async WAL Separation (Vector 3)

Target window: 3-5 weeks  
Goal: enforce WAL-first runtime persistence and move ClickHouse writes fully off runtime hot path.

Exit criteria:
1. Runtime recorder can run in `wal_first` mode with no direct ClickHouse dependency.
2. WAL loader service handles all ClickHouse ingestion asynchronously.
3. Replay lag and backlog are observable and alertable.
4. Disk pressure scenarios have deterministic degradation behavior.

### 3.1 Issue List

| ID | Title | Priority | Estimate | Dependencies | Owner | Status |
|---|---|---|---|---|---|---|
| CE3-01 | Add recorder mode switch (`direct` vs `wal_first`) and defaults | P0 | 2d | None | TBD | ✅ Implemented |
| CE3-02 | Implement strict WAL-first runtime path in RecorderService | P0 | 3d | CE3-01 | TBD | ✅ Implemented |
| CE3-05 | Add WAL disk pressure controls and backpressure policy | P0 | 2d | CE3-02 | TBD | ✅ Implemented |
| CE3-03 | Scale out WAL loader workers and shard assignment policy | P1 | 3d | CE3-02 | TBD | 🔄 TODO |
| CE3-04 | Define replay safety contract (ordering + dedup + manifest) | P0 | 3d | CE3-02 | TBD | 🔄 TODO |
| CE3-06 | Add WAL SLO metrics, alerts, and dashboards | P1 | 2d | CE3-02, CE3-05 | TBD | 🔄 TODO |
| CE3-07 | Outage drills: ClickHouse down, slow, and WAL growth recovery | P0 | 3d | CE3-03, CE3-04, CE3-05 | TBD | 🔄 TODO |

### 3.2 Issue Specs

#### CE3-01 - Add recorder mode switch (`direct` vs `wal_first`) and defaults
- Scope:
  - Add runtime config for recorder mode.
  - Define cluster default as `wal_first`.
- Deliverables:
  - Config flags, docs, startup log banner for active mode.
- Acceptance criteria:
  - Mode is visible in metrics/logs and tested in unit tests.

#### CE3-02 - Implement strict WAL-first runtime path in RecorderService
- Scope:
  - Ensure runtime path never blocks on ClickHouse network I/O in `wal_first`.
  - Keep existing direct mode for compatibility.
- Deliverables:
  - Recorder implementation updates and regression tests.
- Acceptance criteria:
  - With ClickHouse unavailable, runtime continues and WAL persists events.
  - No synchronous ClickHouse calls in wal-first path.

#### CE3-03 - Scale out WAL loader workers and shard assignment policy
- Scope:
  - Define how multiple loaders consume WAL without duplication.
  - Implement shard ownership or file-claim protocol.
- Deliverables:
  - Loader coordination logic and integration tests.
- Acceptance criteria:
  - Two or more loaders process backlog without duplicate inserts.

#### CE3-04 - Define replay safety contract (ordering + dedup + manifest)
- Scope:
  - Standardize replay ordering guarantees and dedup keys.
  - Formalize manifest state transitions.
- Deliverables:
  - Contract doc, loader checks, replay tests.
- Acceptance criteria:
  - Replaying same WAL batch twice does not create duplicate business records.

#### CE3-05 - Add WAL disk pressure controls and backpressure policy
- Scope:
  - Add thresholds for warning/critical disk states.
  - Define strategy for overload (`drop/degrade/halt`) by topic criticality.
- Deliverables:
  - Disk monitor hooks, policy config, failure drills.
- Acceptance criteria:
  - Disk pressure events trigger configured policy and alert.

#### CE3-06 - Add WAL SLO metrics, alerts, and dashboards
- Scope:
  - Expose backlog size, replay lag, replay throughput, replay error rate, drain ETA.
- Deliverables:
  - Metrics definitions, alert rules, dashboard updates.
- Acceptance criteria:
  - Operators can detect and quantify replay health from dashboard only.

#### CE3-07 - Outage drills: ClickHouse down, slow, and WAL growth recovery
- Scope:
  - Add repeatable drills for outage and recovery paths.
  - Validate recovery time and data integrity.
- Deliverables:
  - Drill scripts and test reports.
- Acceptance criteria:
  - Recovery drills pass with no silent data loss and bounded recovery time.

## 4. Execution Order (Updated 2026-02-21)

Completed steps are marked ✅; remaining steps start from first 🔄 item.

**CE-M2** (Gateway):
✅ CE2-01 → ✅ CE2-02 → ✅ CE2-03 → ✅ CE2-04 → ✅ CE2-05 → ✅ CE2-06 → ✅ CE2-10 → ✅ CE2-12
→ 🔄 CE2-07 → 🔄 CE2-09 → 🔄 CE2-11 → 🔄 CE2-08

**CE-M3** (WAL-First):
✅ CE3-01 → ✅ CE3-02 → ✅ CE3-05
→ 🔄 CE3-04 → 🔄 CE3-03 → 🔄 CE3-06 → 🔄 CE3-07

## 5. Definition of Done (Applies to All Issues)

1. Code merged with tests and lint passing.
2. Metrics/logging added for new behavior.
3. Operational docs and runbook updated.
4. Failure mode and rollback behavior explicitly verified.

## 6. Review Checklist (Deliverables + Validation)

Use this section as the operator-facing review list during rollout.

### 6.1 Deliverables

**Implemented**:
- [✅] [CE2-01] `OrderIntent` wire schema with `idempotency_key`, `ttl`, `trace_id` (`gateway/channel.py`)
- [✅] [CE2-02] `LocalIntentChannel` with ack/nack/DLQ (`gateway/channel.py`)
- [✅] [CE2-03] `GatewayService` with 7-step dispatch pipeline (`gateway/service.py`)
- [✅] [CE2-04] `ExposureStore` with atomic CAS and memory bound CE2-12 (`gateway/exposure.py`)
- [✅] [CE2-05] `IdempotencyStore` with persist/load (`gateway/dedup.py`)
- [✅] [CE2-06] `GatewayPolicy` FSM (`gateway/policy.py`)
- [✅] [CE2-10] Enqueue-only channel boundary enforced by async design
- [✅] [CE2-12] `_max_symbols` bound + zero-balance eviction + `ExposureLimitError` (`gateway/exposure.py`)
- [✅] [CE3-01] `RecorderMode` enum + env flag (`recorder/mode.py`)
- [✅] [CE3-02] `WALFirstWriter` — no ClickHouse calls (`recorder/wal_first.py`)
- [✅] [CE3-05] `DiskPressureMonitor` daemon (`recorder/disk_monitor.py`)

**Still TODO**:
- [ ] [CE2-07] Gateway metrics/SLO dashboard.
- [ ] [CE2-09] Gateway HA controller + leader lease adapter + failover runbook.
- [ ] [CE2-11] Quote schema guard (`v1`) + startup/runtime validation.
- [ ] [CE2-08] Multi-runner chaos test.
- [ ] [CE3-04] Replay safety contract (ordering + dedup + manifest) tests.
- [ ] [CE3-03] Scale-out loader workers with shard-claim integration tests.
- [ ] [CE3-06] WAL SLO metrics + dashboard.
- [ ] [CE3-07] Outage drill scripts and recovery report template.

### 6.2 Validation (TODO)

- [ ] [V-GW-EXPOSURE-LIMIT] Add 10,001 unique symbols, verify `ExposureLimitError` raised after zero-balance eviction (unit test added 2026-02-21 ✅).
- [ ] [V-GW-FAILOVER] Kill active gateway and verify standby promotes without duplicate orders.
- [ ] [V-GW-IDEMPOTENCY] Replay same `idempotency_key` intents and verify no second broker dispatch.
- [ ] [V-CALLBACK-LATENCY] Confirm callback thread P99 latency within budget and no heavy parsing in callback stack.
- [ ] [V-QUOTE-V1] Inject schema mismatch payload and verify reject + alert + metric emission.
- [ ] [V-WAL-OUTAGE] Bring ClickHouse down, verify runtime continues and WAL grows; restore and verify drain.
- [ ] [V-WAL-DUP] Re-run same WAL shard batch and verify dedup prevents duplicate business records.

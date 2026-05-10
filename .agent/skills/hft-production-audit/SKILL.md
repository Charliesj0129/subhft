---
name: hft-production-audit
description: Use when performing a multi-plane runtime safety audit, hunting blind spots across the 7 runtime planes, or after a production incident to verify all planes are instrumented and fail-safe.
---

# HFT Production Audit

Systematic audit protocol for the 7 runtime planes. Derived from the ae243a08 audit pattern (13 fixes across 7 planes, 2026-04-12).

## When to Use

- Before a major release or go-live window
- After a production incident or near-miss
- When adding a new runtime component that touches multiple planes
- Periodic quarterly audit (pair with `make reliability-monthly-pack`)

## Audit Matrix

Sweep each plane in order. For every plane, check **Instrumentation**, **Fail-Safety**, and **Backpressure**.

### Plane 1: Control (`services/system.py`, `services/bootstrap.py`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| Loop lag monitoring | `_supervise()` emits loop lag metric | `grep "loop_lag" observability/metrics.py` |
| Queue depth alerts | All bounded queues have depth gauges | `grep "queue_depth\|queue_size" observability/metrics.py` |
| Service liveness | Each service task has supervision | `services/system.py::_supervise()` |
| HALT enforcement | HALT blocks new orders, allows cancels | `risk/storm_guard.py` state matrix |

### Plane 2: Market Data (`feed_adapter/`, `services/market_data.py`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| Drop rate detector | Sliding window burst detection on raw_queue | `services/market_data.py` |
| Subscription accounting | Round-robin slot count matches actual | `feed_adapter/subscription_state.py` |
| Reconnect backoff | Exponential backoff, flap detection | `HFT_RECONNECT_BACKOFF_S`, `HFT_QUOTE_FLAP_*` |
| Feed gap → HALT | `HFT_STORMGUARD_FEED_GAP_HALT_S` fires correctly | StormGuard integration test |

### Plane 3: Feature (`feature/engine.py`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| Schema version match | `lob_shared_v3` is default, 27 features | `feature/registry.py` |
| Warmup guard | Features report NaN/zero before warmup completes | Feature engine unit tests |
| Rust/Python parity | If `HFT_FEATURE_ENGINE_BACKEND=rust`, outputs match Python | CI parity gate |

### Plane 4: Decision (`strategy/runner.py`, `risk/engine.py`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| RiskFeedback completeness | `side` field present, pending counter drains on HALT | `risk/engine.py` |
| Recovery position visibility | Strategy sees positions before first fill | `strategy/runner.py` |
| Pending exposure tracking | No leak on reject/timeout | `risk/engine.py` pending counters |

### Plane 5: Execution (`order/adapter.py`, `execution/`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| Typed-intent identity | OrderCommand preserves intent fields | `order/adapter.py` |
| Adapter rejection feedback | Rejected orders emit feedback to risk | `order/adapter.py` DLQ path |
| Checkpoint persistence | Position checkpoints written on schedule | `execution/checkpoint.py` |
| Startup recon | Positions reconciled at boot | `execution/startup_recon.py` |

### Plane 6: Persistence (`recorder/`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| WAL fallback | ClickHouse failure routes to WAL | `recorder/writer.py` |
| WAL timestamp parsing | Batched timestamps parsed correctly | `recorder/worker.py` |
| DLQ monitoring | Dead-letter files tracked, metric emitted | `make wal-dlq-status` |

### Plane 7: Observability (`observability/`, `risk/storm_guard.py`)

| Check | What to verify | Command / location |
|-------|----------------|--------------------|
| Metric completeness | Every queue, every stage has latency + depth | `observability/metrics.py` |
| Alert rules exist | Prometheus rules cover all CRITICAL paths | `config/alerts/rules.yaml` |
| Health endpoint live | `curl :8080/healthz` returns 200 | `make pre-market-check` |

## Execution Protocol

```bash
# 1. Run automated gates
make pre-market-check
make check              # lint + typecheck + discipline + dependency-boundary
make test               # unit tests
make drill-ck-down      # ClickHouse failover drill

# 2. Manual sweep per plane (use checklist above)
# Open each plane's key file, search for the check items

# 3. Document findings
# Record in commit message: "fix(runtime): production-grade audit — N fixes across M planes"
```

## Output Format

Commit message pattern:
```
fix(runtime): production-grade audit — {N} fixes across {M} runtime planes

Plane-by-plane:
- Control: {findings}
- Market Data: {findings}
- Feature: {findings}
- Decision: {findings}
- Execution: {findings}
- Persistence: {findings}
- Observability: {findings}
```

## Anti-Patterns

- Do NOT audit only the plane where the incident occurred — sweep all 7
- Do NOT skip persistence/observability planes — they cause silent data loss
- Do NOT merge audit fixes across multiple commits — one atomic commit per audit

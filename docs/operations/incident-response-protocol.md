# Incident Response Protocol (WU-12)

Standard operating procedures for HFT Platform production incidents.

## Severity Classification

| Priority | Criteria | Examples | Response SLA |
|---|---|---|---|
| **P0 - Critical** | Active financial loss or complete system failure | StormGuard HALT, position mismatch > 5%, kill switch triggered, all feeds down | 5 min acknowledge, 15 min first response |
| **P1 - High** | Degraded trading capability or data loss risk | Single feed gap > 30s, ClickHouse write failures, reconciliation drift > 1%, WAL backlog > 1GB | 15 min acknowledge, 30 min first response |
| **P2 - Medium** | Partial degradation, no immediate financial impact | Elevated queue depths, single symbol feed stale, recorder latency spike, non-critical service restart | 30 min acknowledge, 2h first response |
| **P3 - Low** | Cosmetic or informational, no trading impact | Grafana dashboard error, metrics cardinality warning, non-production environment issue | Next business day |

Autonomy alert severity mapping:
- `WARN`: strategy quarantine, single dependency wobble, evidence pack ready.
- `HIGH`: platform `reduce-only`, intraday reconciliation drift, dependency degradation with trading impact.
- `CRITICAL`: `HALT`, force-flat failure, broker/account state not trustworthy.

## Escalation Timeline

| Time since detection | Action |
|---|---|
| T+0 | Alert fires. On-call engineer acknowledges. |
| T+5 min | P0: If not acknowledged, auto-escalate to secondary on-call. |
| T+15 min | P0: If no mitigation in progress, escalate to engineering lead. P1: If not acknowledged, escalate to secondary. |
| T+30 min | P0: Escalate to CTO. P1: If no mitigation, escalate to engineering lead. |
| T+1 hour | P0/P1: Incident commander assembled. Status update to all stakeholders. |
| T+4 hours | Post-incident review scheduled regardless of resolution status. |

## Communication Channels

| Channel | Purpose | Who |
|---|---|---|
| Telegram (critical group) | P0 real-time coordination | On-call, engineering lead, CTO |
| Slack `#hft-incidents` | P0/P1 incident thread | Engineering team |
| Slack `#hft-alerts` | P2/P3 automated alerts | All engineers |
| Email | Post-incident reports | Full team + stakeholders |
| Phone | P0 escalation when unacknowledged | On-call chain |

## Decision Trees

### 1. StormGuard HALT

```
StormGuard enters HALT state
|
+-- Verify via metrics: hft_storm_guard_state == HALT
|
+-- Are new orders blocked?
|   +-- YES (expected) --> proceed
|   +-- NO --> EMERGENCY: kill switch immediately
|
+-- Identify trigger cause (check logs: storm_guard.py)
|   +-- Market-wide volatility spike --> wait for conditions to normalize
|   +-- Feed data anomaly (stale/corrupt) --> investigate feed adapter
|   +-- Internal error (risk engine panic) --> investigate error, restart if needed
|
+-- Can we safely resume?
|   +-- YES --> Reset StormGuard state, monitor for 5 min before full resume
|   +-- NO --> Keep HALT, escalate to P0 if not already
|
+-- Post-incident: log trigger cause, review thresholds
```

### 2. Reconciliation Mismatch

```
Reconciliation drift detected
|
+-- First observation only?
|   +-- YES --> WARN only, preserve evidence, watch for persistence/growth
|   +-- NO --> move platform to reduce-only and investigate immediately
|
+-- Identify source
|   +-- Missed fill event --> check broker execution logs, replay if available
|   +-- Double-counted fill --> check idempotency keys in execution pipeline
|   +-- Position sync error --> force reconciliation from broker account query
|
+-- Remediation
|   +-- Run manual reconciliation: hft reconcile --force --symbol <SYM>
|   +-- If broker position differs: broker is source of truth, adjust internal state
|   +-- Log all adjustments with full audit trail
|   +-- When clean: clear manual lock with `hft ops rearm-platform`
```

### 3. ClickHouse Down

```
ClickHouse connection failure
|
+-- Is HFT_RECORDER_MODE=wal_first?
|   +-- YES --> WAL absorbs writes, P2 severity, fix within hours
|   +-- NO --> Data loss risk, P1 severity
|
+-- Check ClickHouse health
|   +-- docker exec clickhouse clickhouse-client --query "SELECT 1"
|   +-- Check disk space: df -h /var/lib/clickhouse
|   +-- Check logs: docker compose logs clickhouse
|
+-- Common causes
|   +-- OOM kill --> increase memory limit, check query load
|   +-- Disk full --> run TTL cleanup, expand storage
|   +-- Corrupt table --> detach/attach affected partition
|
+-- Recovery
|   +-- Restart: docker compose restart clickhouse
|   +-- Verify: run health check query
|   +-- Replay WAL: docker compose run --rm wal-loader
|   +-- Verify data continuity in hft.market_data
```

### 4. Feed Gap

```
Market data feed gap detected (no ticks for > N seconds)
|
+-- Check scope
|   +-- All symbols --> broker connection issue, P0
|   +-- Single symbol --> symbol-specific issue, P2
|   +-- Multiple symbols (same exchange) --> exchange segment issue, P1
|
+-- Diagnose
|   +-- Check broker session: is WebSocket/API connected?
|   +-- Check normalizer metrics: are raw callbacks arriving?
|   +-- Check network: can we reach broker API endpoints?
|
+-- Remediation
|   +-- Broker disconnect --> trigger reconnect (auto or manual)
|   +-- Exchange halt --> wait, log expected gap, suppress false alerts
|   +-- Network issue --> failover if available, escalate to infra
|
+-- After recovery
|   +-- Verify data continuity
|   +-- Check if StormGuard triggered during gap
|   +-- Review if any orders were in-flight during gap
```

### 5. Kill Switch Activation

```
Kill switch activated (manual or automated)
|
+-- ALL new orders blocked immediately
+-- ALL open orders: cancel requested
|
+-- Verify kill state
|   +-- Metrics: hft_kill_switch_active == 1
|   +-- Logs: confirm no orders accepted after activation
|
+-- Cancel verification
|   +-- Query broker for all open orders
|   +-- Confirm all cancels acknowledged
|   +-- If any cancel fails --> manual intervention via broker UI
|
+-- Root cause investigation
|   +-- Why was kill switch needed?
|   +-- Document trigger and timeline
|
+-- Recovery (ONLY after root cause identified and fixed)
|   +-- Reset kill switch
|   +-- Resume with reduced position limits for 30 min
|   +-- Monitor closely, full limits after stable period
```

## Post-Incident Review

Required for all P0 and P1 incidents within 48 hours:

1. **Timeline**: Minute-by-minute reconstruction from detection to resolution.
2. **Root cause**: Technical root cause (not "human error").
3. **Impact**: Financial impact, data loss, duration of degradation.
4. **Detection**: How was the incident detected? Could we detect faster?
5. **Response**: What went well? What could improve?
6. **Action items**: Concrete follow-ups with owners and deadlines.

Document in `docs/operations/postmortems/YYYY-MM-DD-<title>.md`.

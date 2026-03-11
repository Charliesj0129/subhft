---
skill: soak-report-analysis
version: 1
description: |
  Analyze soak test reports, identify degradation trends, and generate operational insights.
  Trigger on: "soak report", "daily report analysis", "health check analysis", "operational status", "system stability".
runtime_plane: Observability
hft_laws: [Async]
---

# Skill: soak-report-analysis

## When to Use

Use this skill when:
- Reviewing daily or weekly soak test acceptance reports.
- Diagnosing why a soak check failed or degraded.
- Preparing an operational status summary for the team.
- Comparing system health across multiple days to detect trends.
- Investigating service restarts, feed reconnect failures, or WAL backlog growth.
- Validating system stability before or after a deployment.

Trigger keywords: `soak`, `daily report`, `health check`, `operational status`, `system stability`, `deployment health`, `soak acceptance`.

## Report Locations

| Report Type | Path Pattern | Format |
|-------------|-------------|--------|
| Daily soak | `outputs/soak_reports/daily/soak_report_YYYY-MM-DD.json` | JSON |
| Weekly summary | `outputs/soak_reports/weekly/weekly_YYYY-WNN.json` | JSON |
| Canary evaluation | `outputs/soak_reports/canary/canary_YYYY-MM-DD.json` | JSON |
| Callback latency | `outputs/callback_latency/latency_YYYY-MM-DD.json` | JSON |
| Monthly reliability | `outputs/reliability/monthly/reliability_YYYY-MM.json` | JSON |

## Analysis Steps

### Step 1: Locate Latest Reports

```bash
# Find the most recent daily soak report
ls -t outputs/soak_reports/daily/*.json | head -1

# Find the last 7 daily reports for trend analysis
ls -t outputs/soak_reports/daily/*.json | head -7
```

### Step 2: Load and Parse JSON

Read the JSON file. Key fields to extract:
- `overall_pass`: boolean — did the soak session pass all checks?
- `checks`: dict — individual check results with `pass`, `value`, `threshold` fields.
- `timestamp`: ISO 8601 — when the report was generated.
- `duration_seconds`: int — how long the soak session ran.
- `services`: dict — per-service health status.

### Step 3: Identify Non-Pass Checks

For each check where `pass == false`:
1. Note the check name, observed value, and threshold.
2. Apply root cause diagnosis rules (see below).
3. Assign severity: CRITICAL, HIGH, MEDIUM, or LOW.

### Step 4: Root Cause Diagnosis Rules

| Failed Check | Likely Cause | Next Step |
|-------------|-------------|-----------|
| `service_restart_count > 0` | OOM kill, unhandled panic, Docker policy | `docker logs --since 24h hft-engine`, check `dmesg` for OOM |
| `feed_reconnect_failure_ratio > 0.20` | Shioaji/Fubon API issue, network | Check broker status, session logs, network connectivity |
| `session_conflict == true` | Duplicate instance with same credentials | Verify single-instance constraint, check lock key |
| `stormguard_halt_count > 0` | Risk threshold breach, volatility spike | Review StormGuard FSM transitions, market data |
| `wal_backlog_files > 200` | ClickHouse slow, disk pressure | `du -sh` on WAL dir, ClickHouse `system.metrics` |
| `recorder_insert_failure_ratio > 0.005` | Schema mismatch, connection timeout | ClickHouse error log, recorder worker logs |
| `first_quote_received == false` | Holiday, weekend, market closure | TWSE/OTC calendar check |
| `execution_uptime < 0.99` | Gateway/router crash | Gateway health endpoint, service restart count |

### Step 5: Trend Analysis

Compare the current report with the previous N reports (default N=7):

1. For each numeric metric, compute the trend direction (improving, stable, degrading).
2. Flag any metric with 3+ consecutive degradations as a **sustained regression**.
3. Compute a rolling average and compare today's value against it.

### Step 6: Generate Executive Summary

Produce a risk score from 0 (healthy) to 100 (critical):

| Condition | Score Contribution |
|-----------|-------------------|
| All checks pass, no trends | 0 |
| Per MEDIUM issue | +10 |
| Per HIGH issue | +25 |
| Per CRITICAL issue | +40 |
| Sustained regression (3+ days) | +15 per metric |

Cap at 100.

### Step 7: Produce Action Items

Order by severity. Each action item includes:
- **Severity**: CRITICAL / HIGH / MEDIUM / LOW
- **What**: Description of the issue
- **Why**: Impact if unaddressed
- **How**: Specific remediation steps
- **Owner suggestion**: Which team role should handle it

## Example Output

```
# Soak Report Analysis — 2026-03-11

## Executive Summary
System health is GOOD. 6/7 checks passed. WAL backlog elevated at 180 files
(threshold 200) — approaching limit but not breached. Feed reconnect ratio
stable at 5%. No service restarts.

## Risk Score: 15/100
One MEDIUM-severity trend: WAL backlog has grown for 3 consecutive days
(120 → 150 → 180). ClickHouse insert latency may be increasing.

## Trend Analysis (7-day)
| Metric                  | 7d Avg | Today | Trend     |
|------------------------|--------|-------|-----------|
| Service restarts       | 0.0    | 0     | Stable    |
| Feed reconnect ratio   | 4.8%   | 5.1%  | Stable    |
| WAL backlog            | 130    | 180   | Degrading |
| Recorder failure ratio | 0.1%   | 0.1%  | Stable    |
| Callback P99 latency   | 12ms   | 14ms  | Stable    |

## Issues & Root Cause
### Issue 1: WAL backlog sustained growth
- **Severity**: MEDIUM
- **Metric**: wal_backlog_files = 180 (threshold: 200)
- **Trend**: 3-day consecutive increase (120 → 150 → 180)
- **Root Cause**: ClickHouse merge backlog increasing — `system.merges`
  shows 12 active merges vs. normal 3-5.
- **Evidence**: `SELECT count() FROM system.merges` returned 12.

## Recommendations
1. [MEDIUM] Investigate ClickHouse merge backlog — check
   `max_concurrent_merges` setting and disk IO utilization.
2. [LOW] Set up a Prometheus alert for `wal_backlog_files > 150`
   as an early warning.
```

## Integration with Other Skills

- **`troubleshoot-metrics`**: Use for live Prometheus queries when a soak check fails and you need current metric values rather than report snapshots.
- **`clickhouse-queries`**: Use for verifying data integrity, checking ClickHouse system tables (`system.merges`, `system.metrics`), and validating recorder health.

## Cross-References

- Soak test runner: `scripts/soak_acceptance.py` (generates daily reports)
- StormGuard FSM: `src/hft_platform/risk/storm_guard.py`
- Recorder pipeline: `src/hft_platform/recorder/` (worker, batcher, writer, wal)
- WAL archive cleanup: `make wal-archive-cleanup`
- Disk crisis runbook: `docs/runbooks/disk-crisis-sop.md`
- Data retention policy: `docs/operations/data-retention-policy.md`

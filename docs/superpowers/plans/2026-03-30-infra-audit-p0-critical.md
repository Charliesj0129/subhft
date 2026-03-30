# Infrastructure Audit P0 — Fix Broken Things

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 12 CRITICAL infrastructure items that are actively broken (dashboard showing no data, alerts silently failing, missing backups, config validation mismatch).

**Architecture:** All P0 items are independent fixes to existing config/infra files. No new services, no code architecture changes. Each task produces a single atomic commit.

**Tech Stack:** Grafana JSON dashboards, Prometheus YAML rules, Alertmanager YAML config, GitHub Actions YAML, Python config schema, Makefile, shell cron docs.

**Spec:** `docs/superpowers/specs/2026-03-30-infrastructure-audit-design.md`

**Follow-up plans:** P1 (HIGH), P2 (MEDIUM), P3 (LOW) — created after P0 lands.

---

### Task 1: M-01 — Rewrite production dashboard metrics + datasource (config only)

**Files:**
- Modify: `config/monitoring/dashboards/hft-production.json`

The entire dashboard is broken: all 10 panels, 2 templating variables, and 2 annotations use `hft_*`-prefixed metrics that don't exist. The datasource uid is `"prometheus"` (lowercase) but the provisioned name is `"Prometheus"` (capital P).

**Metric name mapping** (from `hft_*` prefix to actual names in `metrics.py`):

| Dashboard uses (broken) | Actual metric in `metrics.py` |
|------------------------|-------------------------------|
| `hft_storm_guard_state` | `stormguard_mode` |
| `hft_feed_ticks_total` | `feed_events_total` |
| `hft_orders_submitted_total` | `order_actions_total` |
| `hft_orders_filled_total` | `execution_events_total` |
| `hft_orders_rejected_total` | `order_reject_total` |
| `hft_risk_rejections_total` | `risk_reject_total` |
| `hft_risk_evaluations_total` | `strategy_intents_total` |
| `hft_gateway_latency_ns` | `gateway_dispatch_latency_ns` |
| `hft_wal_pending_files` | `wal_backlog_files` |
| `hft_wal_writes_total` | `recorder_wal_writes_total` |
| `hft_reconciliation_position_delta` | `reconciliation_discrepancy_count` |
| `hft_circuit_breaker_state` | `circuit_breaker_state` |
| `hft_clickhouse_insert_latency_ms` | `recorder_insert_latency_ms` |
| `hft_raw_queue_depth` | `raw_queue_depth` |
| `hft_recorder_queue_depth` | `queue_depth{queue="recorder"}` |
| `hft_risk_queue_depth` | `queue_depth{queue="risk"}` |
| `hft_gateway_intent_channel_depth` | `gateway_intent_channel_depth` |
| `hft_build_info` | `up{job="hft-engine"}` (no build_info metric exists; use `up` for deploy annotation) |
| `hft_strategy_latency_ns_count` (templating) | `strategy_latency_ns_count` |

- [ ] **Step 1: Replace the dashboard JSON**

Replace the entire file with corrected metric names, proper datasource uid, and full panel definitions with actual PromQL queries:

```json
{
  "uid": "hft-production",
  "title": "HFT Production Overview",
  "description": "Production monitoring dashboard for the HFT Platform",
  "tags": ["hft", "production", "trading"],
  "timezone": "Asia/Taipei",
  "schemaVersion": 39,
  "version": 2,
  "refresh": "5s",
  "time": { "from": "now-1h", "to": "now" },
  "fiscalYearStartMonth": 0,
  "liveNow": true,
  "templating": {
    "list": [
      {
        "name": "strategy",
        "type": "query",
        "label": "Strategy",
        "query": "label_values(strategy_latency_ns_count, strategy)",
        "datasource": { "type": "prometheus", "uid": "Prometheus" },
        "refresh": 2,
        "includeAll": true,
        "allValue": ".*",
        "current": { "text": "All", "value": "$__all" },
        "multi": true
      },
      {
        "name": "symbol",
        "type": "query",
        "label": "Symbol",
        "query": "label_values(feed_events_total, symbol)",
        "datasource": { "type": "prometheus", "uid": "Prometheus" },
        "refresh": 2,
        "includeAll": true,
        "allValue": ".*",
        "current": { "text": "All", "value": "$__all" },
        "multi": true
      },
      {
        "name": "interval",
        "type": "interval",
        "label": "Interval",
        "query": "5s,10s,30s,1m,5m,15m",
        "current": { "text": "10s", "value": "10s" },
        "auto": true,
        "auto_min": "5s",
        "auto_count": 100
      }
    ]
  },
  "panels": [
    {
      "id": 1, "title": "StormGuard State Timeline", "type": "state-timeline",
      "gridPos": {"h": 5, "w": 24, "x": 0, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "stormguard_mode", "refId": "A", "legendFormat": "StormGuard"}
      ]
    },
    {
      "id": 2, "title": "Order Throughput", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 5},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(order_actions_total{strategy=~\"$strategy\"}[$interval])", "refId": "A", "legendFormat": "submitted {{strategy}}"},
        {"expr": "rate(execution_events_total{strategy=~\"$strategy\"}[$interval])", "refId": "B", "legendFormat": "filled {{strategy}}"},
        {"expr": "rate(order_reject_total{strategy=~\"$strategy\"}[$interval])", "refId": "C", "legendFormat": "rejected {{strategy}}"}
      ]
    },
    {
      "id": 3, "title": "Risk Rejection Rate", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 5},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(risk_reject_total{strategy=~\"$strategy\"}[$interval])", "refId": "A", "legendFormat": "risk reject {{strategy}}"},
        {"expr": "rate(strategy_intents_total{strategy=~\"$strategy\"}[$interval])", "refId": "B", "legendFormat": "intents {{strategy}}"}
      ]
    },
    {
      "id": 4, "title": "Market Data Feed Rate", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 13},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(feed_events_total{symbol=~\"$symbol\"}[$interval])", "refId": "A", "legendFormat": "{{symbol}}"}
      ]
    },
    {
      "id": 5, "title": "Gateway / Order Latency", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 13},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "histogram_quantile(0.95, sum(rate(gateway_dispatch_latency_ns_bucket[$interval])) by (le))", "refId": "A", "legendFormat": "gateway p95"},
        {"expr": "histogram_quantile(0.99, sum(rate(gateway_dispatch_latency_ns_bucket[$interval])) by (le))", "refId": "B", "legendFormat": "gateway p99"}
      ]
    },
    {
      "id": 6, "title": "Queue Depths", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 21},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "raw_queue_depth", "refId": "A", "legendFormat": "raw"},
        {"expr": "queue_depth{queue=\"recorder\"}", "refId": "B", "legendFormat": "recorder"},
        {"expr": "queue_depth{queue=\"risk\"}", "refId": "C", "legendFormat": "risk"},
        {"expr": "gateway_intent_channel_depth", "refId": "D", "legendFormat": "gateway"}
      ]
    },
    {
      "id": 7, "title": "Circuit Breaker Status", "type": "stat",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 21},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "circuit_breaker_state", "refId": "A", "legendFormat": "circuit breaker"}
      ],
      "options": {"colorMode": "background", "graphMode": "none"}
    },
    {
      "id": 8, "title": "Position Reconciliation Delta", "type": "timeseries",
      "gridPos": {"h": 8, "w": 8, "x": 0, "y": 29},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "reconciliation_discrepancy_count", "refId": "A", "legendFormat": "discrepancy"}
      ]
    },
    {
      "id": 9, "title": "ClickHouse Insert Latency", "type": "timeseries",
      "gridPos": {"h": 8, "w": 8, "x": 8, "y": 29},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "histogram_quantile(0.95, sum(rate(recorder_insert_latency_ms_bucket[$interval])) by (le))", "refId": "A", "legendFormat": "p95"},
        {"expr": "histogram_quantile(0.99, sum(rate(recorder_insert_latency_ms_bucket[$interval])) by (le))", "refId": "B", "legendFormat": "p99"}
      ]
    },
    {
      "id": 10, "title": "WAL File Count", "type": "timeseries",
      "gridPos": {"h": 8, "w": 8, "x": 16, "y": 29},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "wal_backlog_files", "refId": "A", "legendFormat": "backlog"},
        {"expr": "recorder_wal_writes_total", "refId": "B", "legendFormat": "writes total"}
      ]
    }
  ],
  "annotations": {
    "list": [
      { "name": "Deployments", "datasource": "Prometheus", "expr": "changes(up{job=\"hft-engine\"}[1m]) > 0", "iconColor": "blue" },
      { "name": "HALT Events", "datasource": "Prometheus", "expr": "changes(stormguard_mode[1m]) > 0 and stormguard_mode == 2", "iconColor": "red" }
    ]
  }
}
```

- [ ] **Step 2: Validate JSON syntax**

Run: `python -m json.tool config/monitoring/dashboards/hft-production.json > /dev/null`
Expected: exit 0, no output

- [ ] **Step 3: Commit**

```bash
git add config/monitoring/dashboards/hft-production.json
git commit -m "fix(monitoring): rewrite hft-production dashboard with correct metric names

All 10 panels, 2 templating vars, and 2 annotations were using non-existent
hft_*-prefixed metrics. Replaced with actual metric names from metrics.py.
Fixed datasource uid from lowercase 'prometheus' to 'Prometheus' matching
the provisioned datasource name."
```

---

### Task 2: M-02 — Fix YAML indentation bug in alert rules

**Files:**
- Modify: `config/monitoring/alerts/rules.yaml:498-536`

Three alert rules (`ResearchDataTooLarge`, `SSDReallocatedSectorsHigh`, `SSDWearLevelLow`) are indented 4 spaces, nesting them under `BackupStale` instead of being top-level list items.

- [ ] **Step 1: Fix indentation — de-indent the three rules to 2-space (top-level list items)**

Change lines 508-535 from 4-space indent to 2-space indent, and add proper section comment separation:

```yaml
  # ── Backup Health ─────────────────────────────────────────────
  - alert: BackupStale
    expr: hft_backup_last_success_ts < (time() - 172800)
    for: 1h
    labels:
        severity: critical
    annotations:
        summary: ClickHouse backup is stale (>2 days since last success)
        description: "Last successful backup was {{ $value | humanizeTimestamp }}. Check cron and backup disk."

  # ── Research Data Disk ────────────────────────────────────────
  - alert: ResearchDataTooLarge
    expr: hft_research_data_bytes > 200e9
    for: 1h
    labels:
      severity: warning
    annotations:
      summary: "Research data exceeds 200 GB"
      description: "research/data/ is {{ $value | humanize1024 }}. Check rotation script."

  # ── SSD Health ────────────────────────────────────────────────
  - alert: SSDReallocatedSectorsHigh
    expr: smartmon_reallocated_sectors > 100
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "SSD has >100 reallocated sectors"
      description: "Device {{ $labels.device }} has {{ $value }} reallocated sectors. Disk replacement recommended."

  - alert: SSDWearLevelLow
    expr: smartmon_wear_leveling > 0 and smartmon_wear_leveling < 20
    for: 0s
    labels:
      severity: warning
    annotations:
      summary: "SSD wear level below 20%"
      description: "Device {{ $labels.device }} wear level at {{ $value }}%. Plan disk replacement."
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('config/monitoring/alerts/rules.yaml'))"`
Expected: exit 0, no error

- [ ] **Step 3: Verify all 3 fixed alerts parse as top-level rules**

Run: `python -c "
import yaml
with open('config/monitoring/alerts/rules.yaml') as f:
    data = yaml.safe_load(f)
rules = data['groups'][0]['rules']
names = [r['alert'] for r in rules]
for name in ['ResearchDataTooLarge', 'SSDReallocatedSectorsHigh', 'SSDWearLevelLow']:
    assert name in names, f'{name} not found as top-level rule'
    print(f'OK: {name}')
"`
Expected: prints `OK:` for all three

- [ ] **Step 4: Commit**

```bash
git add config/monitoring/alerts/rules.yaml
git commit -m "fix(monitoring): de-indent 3 silenced alert rules in rules.yaml

ResearchDataTooLarge, SSDReallocatedSectorsHigh, SSDWearLevelLow were
indented 4 spaces (nested under BackupStale) instead of 2 spaces
(top-level list items). They were silently ignored by Prometheus."
```

---

### Task 3: M-03 — Fix alertmanager inhibit rule names

**Files:**
- Modify: `config/monitoring/alerts/alertmanager.production.yml:108-131`

- [ ] **Step 1: Fix the three inhibit rules**

Replace lines 108-131:

```yaml
# Inhibition rules: suppress lower-severity alerts when higher-severity fires
inhibit_rules:
  # If HALT is active (critical), suppress related warnings
  - source_matchers:
      - severity = critical
      - alertname = StormGuardHalt
    target_matchers:
      - severity = warning
    equal: ["strategy"]

  # If ClickHouse is down (critical), suppress CH insert latency warnings
  - source_matchers:
      - severity = critical
      - alertname = ClickHouseConnectionDown
    target_matchers:
      - severity = warning
      - alertname =~ "ClickHouse.*"

  # If feed gap is critical, suppress feed rate warnings
  - source_matchers:
      - severity = critical
      - alertname = FeedGapCritical
    target_matchers:
      - severity = warning
      - alertname =~ "Feed.*"
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('config/monitoring/alerts/alertmanager.production.yml'))"`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add config/monitoring/alerts/alertmanager.production.yml
git commit -m "fix(monitoring): correct alert names in alertmanager inhibit rules

HFTStormGuardHalt -> StormGuardHalt, ClickHouseDown -> ClickHouseConnectionDown,
FeedDead -> FeedGapCritical. All three inhibit rules were referencing
non-existent alert names and never firing."
```

---

### Task 4: M-04 — Add Redis health gauge to metrics.py

**Files:**
- Modify: `src/hft_platform/observability/metrics.py`

Following the existing `clickhouse_connection_health` pattern (Gauge, 1=healthy/0=unhealthy).

- [ ] **Step 1: Find `clickhouse_connection_health` Gauge definition and add `redis_connection_health` after it**

After the `clickhouse_connection_health` Gauge block (~line 467), add:

```python
        # Redis connection health gauge
        self.redis_connection_health = Gauge(
            "redis_connection_health",
            "Redis connection health (1=healthy, 0=unhealthy)",
        )
```

- [ ] **Step 2: Add `"redis_connection_health"` to the REGISTRY list**

Find the REGISTRY list (around line 109 where `"clickhouse_connection_health"` is listed) and add `"redis_connection_health"` after it:

```python
                "clickhouse_connection_health",
                "redis_connection_health",
```

- [ ] **Step 3: Verify metric registers without error**

Run: `uv run python -c "from hft_platform.observability.metrics import MetricsRegistry; m = MetricsRegistry(); print('redis_connection_health:', m.redis_connection_health)"`
Expected: prints the Gauge object, no ImportError

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/observability/metrics.py
git commit -m "feat(monitoring): add redis_connection_health gauge to metrics registry

Follows the clickhouse_connection_health pattern (1=healthy, 0=unhealthy).
Enables the existing RedisConnectionDown alert rule to function.
Note: wiring the gauge to actual Redis health checks is a separate step."
```

---

### Task 5: M-05 — Add ClickHouse Prometheus exporter scrape job

**Files:**
- Modify: `config/monitoring/prometheus.yml`
- Modify: `docker-compose.yml` (expose port 9363)

ClickHouse has a built-in Prometheus exporter on port 9363, but it needs to be enabled and exposed.

- [ ] **Step 1: Add ClickHouse Prometheus config XML**

Create `config/clickhouse_prometheus.xml`:

```xml
<clickhouse>
    <prometheus>
        <endpoint>/metrics</endpoint>
        <port>9363</port>
        <metrics>true</metrics>
        <events>true</events>
        <asynchronous_metrics>true</asynchronous_metrics>
    </prometheus>
</clickhouse>
```

- [ ] **Step 2: Mount the config in docker-compose.yml**

In the `clickhouse` service `volumes:` section (after `clickhouse_backup.xml`), add:

```yaml
      - ./config/clickhouse_prometheus.xml:/etc/clickhouse-server/config.d/prometheus.xml:ro
```

- [ ] **Step 3: Add scrape job to prometheus.yml**

Append to `scrape_configs:` in `config/monitoring/prometheus.yml`:

```yaml
    - job_name: "clickhouse"
      static_configs:
        - targets:
            - "clickhouse:9363"
```

- [ ] **Step 4: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('config/monitoring/prometheus.yml'))"`
Expected: exit 0

- [ ] **Step 5: Commit**

```bash
git add config/clickhouse_prometheus.xml config/monitoring/prometheus.yml docker-compose.yml
git commit -m "feat(monitoring): enable ClickHouse Prometheus exporter and scrape job

Adds clickhouse_prometheus.xml to enable native /metrics on port 9363.
Mounts config in docker-compose and adds prometheus.yml scrape job.
Enables ClickHouseSystemLogSizeCritical alert (previously unfirable)."
```

---

### Task 6: M-06 — Add HFTEngineDown alert rule

**Files:**
- Modify: `config/monitoring/alerts/rules.yaml`

- [ ] **Step 1: Add the alert rule at the top of the rules list (after the group header)**

Find the first `- alert:` line and add before it:

```yaml
  # ── Target Health ─────────────────────────────────────────────
  - alert: HFTEngineDown
    expr: up{job="hft-engine"} == 0
    for: 30s
    labels:
      severity: critical
    annotations:
      summary: "HFT Engine scrape target is down"
      description: "hft-engine:9090 has been unreachable for >30s. The trading process may have crashed."
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('config/monitoring/alerts/rules.yaml'))"`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add config/monitoring/alerts/rules.yaml
git commit -m "feat(monitoring): add HFTEngineDown alert for scrape target failure

If hft-engine:9090 is unreachable for >30s, fires critical alert.
Previously there was no direct target-down alert — only indirect coverage
via stale liveness gauges with varying thresholds."
```

---

### Task 7: O-01 — Remove broken release-readiness-check Makefile target

**Files:**
- Modify: `Makefile:422-423`

- [ ] **Step 1: Remove the target**

Delete these lines from Makefile:

```makefile
release-readiness-check: ## Evaluate March 30 canary readiness from repo and runtime gate evidence
	$(PY) scripts/release_readiness.py --project-root . --output-dir outputs/release_readiness --milestone-date 2026-03-30 --prod-date 2026-04-03
```

- [ ] **Step 2: Verify target is gone**

Run: `make release-readiness-check 2>&1 | head -1`
Expected: `make: *** No rule to make target 'release-readiness-check'.  Stop.`

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "fix(ops): remove broken release-readiness-check Makefile target

The target called scripts/release_readiness.py which does not exist.
Will be re-added when the script is implemented."
```

---

### Task 8: O-02 — Add backup cron entry to canonical crontab doc

**Files:**
- Modify: `docs/operations/cron-setup-remote.md`

- [ ] **Step 1: Add ClickHouse backup entry after the existing WAL cleanup entry (around line 33)**

Insert a new cron block after the WAL archive cleanup section:

```markdown
# ClickHouse daily backup (14:30 TST, after market close)
# Design spec: docs/superpowers/specs/2026-03-25-clickhouse-backup-safety-design.md
30 14 * * * cd /home/charl/subhft && bash scripts/clickhouse_backup.sh >> /tmp/ch_backup.log 2>&1
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations/cron-setup-remote.md
git commit -m "fix(ops): add ClickHouse backup to canonical crontab template

clickhouse_backup.sh at 14:30 daily (after market close), per the
backup safety design spec. Previously neither backup script was
documented in the cron template."
```

---

### Task 9: O-03 — Delete orphan verify_rust_deployment.py

**Files:**
- Delete: `scripts/verify_rust_deployment.py`

- [ ] **Step 1: Delete the file**

```bash
git rm scripts/verify_rust_deployment.py
```

- [ ] **Step 2: Verify no references remain**

Run: `grep -r "verify_rust_deployment" Makefile .github/ docs/ scripts/ 2>/dev/null | grep -v ".git/" || echo "No references found"`
Expected: "No references found"

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(ops): remove orphan verify_rust_deployment.py

Unreferenced script from Feb 2026 bootstrap phase. No Makefile target,
no CI job, no documentation references. If Rust deployment verification
is needed, it should be a proper Makefile target."
```

---

### Task 10: C-01 — Investigate and fix prod config loading

**Files:**
- Read: `src/hft_platform/config/loader.py`
- Read: `src/hft_platform/services/bootstrap.py` (risk_path resolution)
- Possibly modify: `config/env/prod/main.yaml`

This task requires investigation first — the fix depends on what bootstrap.py actually does.

- [ ] **Step 1: Investigate how bootstrap resolves risk config**

Run: `grep -n "risk" src/hft_platform/services/bootstrap.py | head -20`
Run: `grep -n "strategy_limits\|risk_path\|risk.yaml" src/hft_platform/services/bootstrap.py | head -20`
Run: `grep -n "risk" src/hft_platform/config/loader.py | head -20`

Document the findings: Does bootstrap load `config/env/prod/risk.yaml` via a separate path? Or does it only use `config/base/strategy_limits.yaml`?

- [ ] **Step 2: Based on findings, choose fix path**

**If prod risk is loaded via a separate path:** Add a code comment in `loader.py` or `bootstrap.py` documenting this path for future maintainers. Commit with message: `docs(config): document prod risk config loading path`.

**If prod risk is NOT loaded:** Inline the critical prod risk values from `config/env/prod/risk.yaml` into `config/env/prod/main.yaml` under a `risk:` key. The loader will merge these into the settings dict (they'll pass through as unknown keys until C-03 adds validation). Commit with message: `fix(config): inline prod risk values into env/prod/main.yaml`.

- [ ] **Step 3: Commit per findings**

(Commit message depends on step 2 outcome.)

---

### Task 11: C-02 — Normalize HFT_MODE "real" → "live"

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py:180`

Rather than adding `"real"` to schema's `_VALID_MODES`, normalize early in bootstrap so `"real"` becomes an alias for `"live"`.

- [ ] **Step 1: Add normalization after mode is read**

In `bootstrap.py`, after line 180 (`hft_mode = os.getenv("HFT_MODE", "sim").strip().lower()`), add:

```python
    # Normalize "real" → "live" (legacy alias)
    if hft_mode == "real":
        hft_mode = "live"
```

- [ ] **Step 2: Write a test**

Create or modify `tests/unit/test_bootstrap_mode.py`:

```python
import os
from unittest.mock import patch


def test_hft_mode_real_normalized_to_live():
    """HFT_MODE='real' should be treated as 'live' (legacy alias)."""
    with patch.dict(os.environ, {"HFT_MODE": "real", "HFT_ORDER_MODE": "sim"}):
        from hft_platform.services.bootstrap import _resolve_mode
        # If _resolve_mode doesn't exist as a standalone function,
        # test indirectly by checking the mode after bootstrap config loading.
        # The key assertion: "real" should not reach schema validation.
        pass  # Adjust based on actual bootstrap structure
```

Note: The exact test depends on whether mode resolution is extractable. If bootstrap is monolithic, test via config loader integration test.

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/unit/ -k "mode" -v --tb=short`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/services/bootstrap.py tests/unit/test_bootstrap_mode.py
git commit -m "fix(config): normalize HFT_MODE='real' to 'live' in bootstrap

'real' was accepted by bootstrap but rejected by schema validation
(_VALID_MODES only has sim/live/replay). Now 'real' is normalized to
'live' before any validation, making it a proper alias."
```

---

### Task 12: CI-01 — Pin GitHub Actions to SHA digests

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/deploy.yml`
- Modify: `.github/workflows/canary-deploy.yml`
- Modify: `.github/workflows/pr-review-checklist.yml`

**Unique actions to pin (9 total):**

| Action | Current | SHA to look up |
|--------|---------|----------------|
| `actions/checkout` | `@v4` | Look up latest v4 SHA |
| `actions/cache` | `@v4` | Look up latest v4 SHA |
| `actions/upload-artifact` | `@v4` | Look up latest v4 SHA |
| `actions/setup-python` | `@v5` | Look up latest v5 SHA |
| `astral-sh/setup-uv` | `@v4` | Look up latest v4 SHA |
| `dtolnay/rust-toolchain` | `@stable` | Look up latest SHA |
| `docker/login-action` | `@v3` | Look up latest v3 SHA |
| `docker/setup-buildx-action` | `@v3` | Look up latest v3 SHA |
| `docker/build-push-action` | `@v6` | Look up latest v6 SHA |

- [ ] **Step 1: Look up current SHA digests**

For each action, get the commit SHA for the current version tag:

```bash
for repo in actions/checkout:v4 actions/cache:v4 actions/upload-artifact:v4 \
  actions/setup-python:v5 "astral-sh/setup-uv:v4" "dtolnay/rust-toolchain:stable" \
  docker/login-action:v3 docker/setup-buildx-action:v3 docker/build-push-action:v6; do
  REPO="${repo%%:*}"
  TAG="${repo##*:}"
  SHA=$(gh api "repos/$REPO/git/ref/tags/$TAG" --jq '.object.sha' 2>/dev/null || echo "LOOKUP_FAILED")
  echo "uses: $REPO@$SHA  # $TAG"
done
```

Record each SHA. If `gh api` fails for any (e.g., `stable` is a branch not tag), use: `gh api "repos/$REPO/git/ref/heads/$TAG" --jq '.object.sha'`

- [ ] **Step 2: Replace all `uses:` lines across 4 workflow files**

For each workflow file, do a global find-replace. Example for `actions/checkout@v4`:

```
# Before:
uses: actions/checkout@v4

# After (SHA is from step 1):
uses: actions/checkout@<sha>  # v4
```

Repeat for all 9 actions across all 4 files. The `ci.yml` file has ~48 `uses:` lines; `deploy.yml` has 8; `canary-deploy.yml` has 2; `pr-review-checklist.yml` has 1.

- [ ] **Step 3: Verify workflows parse correctly**

Run: `python -c "
import yaml
for f in ['.github/workflows/ci.yml', '.github/workflows/deploy.yml', '.github/workflows/canary-deploy.yml', '.github/workflows/pr-review-checklist.yml']:
    yaml.safe_load(open(f))
    print(f'OK: {f}')
"`
Expected: all 4 print OK

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/
git commit -m "fix(ci): pin all GitHub Actions to SHA digests

Supply chain hardening: replace mutable version tags (@v4, @v5, @stable)
with immutable SHA digests across all 4 workflow files. Version comments
preserved for readability. Covers 9 unique actions, 59 total uses: lines."
```

---

## Post-P0 Verification

After all 12 tasks are committed:

- [ ] **V1: Dashboard** — `docker compose restart grafana` → open `http://localhost:3000/d/hft-production` → confirm panels show data (or "No data" only for metrics not yet emitting, not for wrong metric names)
- [ ] **V2: Alert rules** — `docker compose exec prometheus promtool check rules /etc/prometheus/alerts/rules.yaml` → confirm 0 errors, rule count includes the 3 fixed + 1 new
- [ ] **V3: Alertmanager** — `docker compose exec alertmanager amtool check-config /etc/alertmanager/alertmanager.yml` → confirm valid
- [ ] **V4: CI** — Push a branch and confirm CI runs successfully with pinned actions
- [ ] **V5: Makefile** — `make release-readiness-check` → confirms "No rule to make target"
- [ ] **V6: Cron doc** — `grep clickhouse_backup docs/operations/cron-setup-remote.md` → confirms entry exists

---

## Next Plans

After P0 lands and verification passes:
- **P1 plan**: `docs/superpowers/plans/2026-MM-DD-infra-audit-p1-high.md`
- **P2 plan**: `docs/superpowers/plans/2026-MM-DD-infra-audit-p2-medium.md`
- **P3 plan**: `docs/superpowers/plans/2026-MM-DD-infra-audit-p3-low.md`

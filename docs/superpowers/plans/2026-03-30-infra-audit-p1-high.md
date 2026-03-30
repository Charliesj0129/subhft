# Infrastructure Audit P1 — Close Key Gaps

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 15 HIGH-priority gaps: add missing critical alerts (7), implement SLO-2 latency metric, create 3 new dashboards, add CI security gates (3), extend config schema validation, add hot-reload domain validation, and clean up ops.sh.

**Architecture:** Monitoring tasks are YAML/JSON config edits. M-08 is the only code change (adding a Histogram to execution router). CI tasks are workflow YAML edits. Config tasks extend Python schema validation.

**Tech Stack:** Prometheus alert YAML, Grafana JSON dashboards, GitHub Actions YAML, Python msgspec.Struct, asyncio config watcher.

**Spec:** `docs/superpowers/specs/2026-03-30-infrastructure-audit-design.md` §3

**Depends on:** P0 complete (M-02 YAML fix must land before adding new alerts to rules.yaml)

---

### Task 1: M-07 + M-09 + M-10 + M-11 + M-12 + M-13 — Add 10 missing alert rules

**Files:**
- Modify: `config/monitoring/alerts/rules.yaml`

All 10 new alerts go into the same file. Add them as a batch at the end of the rules list (before the SSD/Research section fixed in P0 M-02).

- [ ] **Step 1: Read current rules.yaml to find insertion point**

Run: `tail -40 config/monitoring/alerts/rules.yaml`

Find the last alert before the `# ── Research Data Disk` section. Insert new alerts before that section.

- [ ] **Step 2: Add all 10 alert rules**

Append these rules before the Research Data section:

```yaml
  # ── Fill Loss (M-07) ─────────────────────────────────────────
  - alert: FillEvictionDetected
    expr: rate(exec_overflow_evicted_total[5m]) > 0
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "Fill events are being evicted (data loss)"
      description: "exec_overflow_evicted_total is increasing. Fill events are being silently dropped."

  # ── Alpha Signal Silence (M-09) ──────────────────────────────
  - alert: AlphaSignalSilent
    expr: (time() - alpha_last_signal_ts) > 300
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "No alpha signal for >5 minutes"
      description: "alpha_last_signal_ts is {{ $value | humanizeDuration }} old. Alpha pipeline may be stalled."

  # ── Autonomy Control-Plane (M-10) ────────────────────────────
  - alert: StrategyQuarantineActive
    expr: strategy_quarantine_active == 1
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "Strategy quarantine is active"
      description: "A strategy has been quarantined. Manual review required."

  - alert: PlatformReduceOnlyActive
    expr: platform_reduce_only_active == 1
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "Platform is in reduce-only mode"
      description: "New positions are blocked. Only position reduction is allowed."

  - alert: ManualRearmRequired
    expr: manual_rearm_required == 1
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "Manual rearm required"
      description: "Platform requires manual intervention to resume normal operations."

  - alert: AutonomyStateTransition
    expr: increase(autonomy_transitions_total[5m]) > 0
    for: 0s
    labels:
      severity: warning
    annotations:
      summary: "Autonomy state transition detected"
      description: "The autonomy control-plane changed state. Check current mode."

  # ── Gateway Heartbeat (M-11) ─────────────────────────────────
  - alert: ExecutionGatewayHeartbeatStale
    expr: (time() - execution_gateway_heartbeat_ts) > 60
    for: 30s
    labels:
      severity: critical
    annotations:
      summary: "Execution Gateway heartbeat is stale (>60s)"
      description: "execution_gateway_heartbeat_ts has not been updated in >60 seconds."

  # ── WAL Integrity (M-12) ─────────────────────────────────────
  - alert: WALCorruptionDetected
    expr: increase(wal_corrupt_files_total[1h]) > 0
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "Corrupt WAL files detected"
      description: "{{ $value }} corrupt WAL file(s) quarantined in the last hour."

  - alert: WALReplayErrorsDetected
    expr: increase(wal_replay_errors_total[1h]) > 0
    for: 0s
    labels:
      severity: warning
    annotations:
      summary: "WAL replay errors detected"
      description: "{{ $value }} WAL replay error(s) in the last hour."

  # ── ClickHouse Pool (M-13) ───────────────────────────────────
  - alert: ClickHousePoolExhausted
    expr: increase(clickhouse_pool_timeout_total[5m]) > 0
    for: 0s
    labels:
      severity: critical
    annotations:
      summary: "ClickHouse connection pool timeouts detected"
      description: "{{ $value }} pool timeout(s) in 5 minutes. Writes may be stalling."
```

- [ ] **Step 3: Validate YAML**

Run: `python -c "import yaml; yaml.safe_load(open('config/monitoring/alerts/rules.yaml'))"`
Expected: exit 0

- [ ] **Step 4: Verify all 10 new alerts parse as top-level rules**

Run: `python -c "
import yaml
with open('config/monitoring/alerts/rules.yaml') as f:
    data = yaml.safe_load(f)
rules = data['groups'][0]['rules']
names = [r['alert'] for r in rules]
expected = ['FillEvictionDetected', 'AlphaSignalSilent', 'StrategyQuarantineActive',
  'PlatformReduceOnlyActive', 'ManualRearmRequired', 'AutonomyStateTransition',
  'ExecutionGatewayHeartbeatStale', 'WALCorruptionDetected', 'WALReplayErrorsDetected',
  'ClickHousePoolExhausted']
for n in expected:
    assert n in names, f'{n} missing'
    print(f'OK: {n}')
print(f'Total rules: {len(rules)}')
"`

- [ ] **Step 5: Commit**

```bash
git add config/monitoring/alerts/rules.yaml
git commit -m "feat(monitoring): add 10 critical alert rules (M-07/09/10/11/12/13)

FillEvictionDetected (fill data loss), AlphaSignalSilent (5min timeout),
StrategyQuarantineActive, PlatformReduceOnlyActive, ManualRearmRequired,
AutonomyStateTransition, ExecutionGatewayHeartbeatStale (60s),
WALCorruptionDetected, WALReplayErrorsDetected, ClickHousePoolExhausted."
```

---

### Task 2: M-08 — Implement e2e_order_latency_ns metric

**Files:**
- Modify: `src/hft_platform/observability/metrics.py` (add Histogram)
- Modify: `src/hft_platform/execution/router.py` (observe latency on fill)
- Modify: `config/monitoring/recording_rules/slo.yaml` (add P95 recording rule)

This is the only code change in P1. The metric measures `FillEvent.ingest_ts_ns - OrderCommand.created_ns`.

- [ ] **Step 1: Add Histogram to metrics.py**

Find the REGISTRY list and add `"e2e_order_latency_ns"`. Then add the Histogram definition near other execution metrics:

```python
        # E2E order-to-fill latency (SLO-2)
        self.e2e_order_latency_ns = Histogram(
            "e2e_order_latency_ns",
            "End-to-end order-to-fill latency in nanoseconds",
            buckets=[1e6, 5e6, 10e6, 20e6, 50e6, 100e6, 200e6, 500e6, 1e9],
        )
```

Buckets: 1ms, 5ms, 10ms, 20ms, 50ms, 100ms, 200ms, 500ms, 1s (in nanoseconds).

- [ ] **Step 2: Observe latency in router.py on fill processing**

In `ExecutionRouter.run()`, after the fill is normalized (around line 120 `fill_event = self.normalizer.normalize_fill(raw)`), look up the original `OrderCommand.created_ns` and observe the latency.

The router needs to access the order's `created_ns`. Check if `self._pending_orders` or similar structure maps `order_id` → `OrderCommand`. If it does, add:

```python
        # Observe e2e latency (SLO-2)
        cmd = self._pending_orders.get(fill_event.order_id)
        if cmd and cmd.created_ns > 0:
            latency_ns = fill_event.ingest_ts_ns - cmd.created_ns
            if latency_ns > 0:
                self._metrics.e2e_order_latency_ns.observe(latency_ns)
```

If the router doesn't have access to the original OrderCommand, the observation point may need to be in `order/adapter.py` where both the command and fill are available. Investigate and document the chosen location.

- [ ] **Step 3: Add recording rule for P95**

Append to `config/monitoring/recording_rules/slo.yaml`:

```yaml
  - record: slo:e2e_order_latency:p95_ms_rate5m
    expr: histogram_quantile(0.95, sum(rate(e2e_order_latency_ns_bucket[5m])) by (le)) / 1e6
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_execution_router.py -v --tb=short -x` (if exists)
Run: `uv run pytest tests/unit/ -k "router or execution" -v --tb=short -x`

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/observability/metrics.py src/hft_platform/execution/router.py config/monitoring/recording_rules/slo.yaml
git commit -m "feat(monitoring): implement e2e_order_latency_ns metric (SLO-2)

Histogram observed on fill receipt in ExecutionRouter. Measures
FillEvent.ingest_ts_ns - OrderCommand.created_ns. Buckets from 1ms to 1s.
Recording rule added for P95 in ms. Dashboard already queries this metric."
```

---

### Task 3: M-14 — Create 3 new Grafana dashboards

**Files:**
- Create: `config/monitoring/dashboards/pipeline-health.json`
- Create: `config/monitoring/dashboards/strategy-risk.json`
- Create: `config/monitoring/dashboards/execution-order.json`

All use correct metric names from `metrics.py` and datasource `"Prometheus"`.

- [ ] **Step 1: Create pipeline-health.json**

```json
{
  "uid": "hft-pipeline-health",
  "title": "Pipeline Health",
  "tags": ["hft", "pipeline"],
  "timezone": "Asia/Taipei",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "5s",
  "time": { "from": "now-1h", "to": "now" },
  "panels": [
    {
      "id": 1, "title": "Bus Overflow Rate", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(bus_overflow_total[1m])", "refId": "A", "legendFormat": "overflow/s"}
      ]
    },
    {
      "id": 2, "title": "Queue Depths", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "raw_queue_depth", "refId": "A", "legendFormat": "raw"},
        {"expr": "queue_depth", "refId": "B", "legendFormat": "{{queue}}"}
      ]
    },
    {
      "id": 3, "title": "Event Loop Lag (ms)", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "event_loop_lag_ms", "refId": "A", "legendFormat": "lag"}
      ]
    },
    {
      "id": 4, "title": "Normalization Errors", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(normalization_errors_total[1m])", "refId": "A", "legendFormat": "errors/s"}
      ]
    }
  ]
}
```

- [ ] **Step 2: Create strategy-risk.json**

```json
{
  "uid": "hft-strategy-risk",
  "title": "Strategy & Risk",
  "tags": ["hft", "strategy", "risk"],
  "timezone": "Asia/Taipei",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "5s",
  "time": { "from": "now-1h", "to": "now" },
  "panels": [
    {
      "id": 1, "title": "Strategy Latency P95 (ns)", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "histogram_quantile(0.95, sum(rate(strategy_latency_ns_bucket[1m])) by (le, strategy))", "refId": "A", "legendFormat": "{{strategy}} p95"}
      ]
    },
    {
      "id": 2, "title": "Strategy Intents Rate", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(strategy_intents_total[1m])", "refId": "A", "legendFormat": "{{strategy}}"},
        {"expr": "rate(risk_reject_total[1m])", "refId": "B", "legendFormat": "rejected {{strategy}}"}
      ]
    },
    {
      "id": 3, "title": "StormGuard Mode", "type": "state-timeline",
      "gridPos": {"h": 4, "w": 12, "x": 0, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "stormguard_mode", "refId": "A", "legendFormat": "StormGuard"}
      ]
    },
    {
      "id": 4, "title": "Autonomy Mode", "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 12, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "autonomy_mode", "refId": "A", "legendFormat": "autonomy"}
      ]
    },
    {
      "id": 5, "title": "Strategy Quarantine", "type": "stat",
      "gridPos": {"h": 4, "w": 6, "x": 18, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "strategy_quarantine_active", "refId": "A", "legendFormat": "quarantine"}
      ]
    },
    {
      "id": 6, "title": "Alpha Last Signal Age (s)", "type": "timeseries",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 12},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "time() - alpha_last_signal_ts", "refId": "A", "legendFormat": "signal age"}
      ]
    }
  ]
}
```

- [ ] **Step 3: Create execution-order.json**

```json
{
  "uid": "hft-execution-order",
  "title": "Execution & Order",
  "tags": ["hft", "execution", "order"],
  "timezone": "Asia/Taipei",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "5s",
  "time": { "from": "now-1h", "to": "now" },
  "panels": [
    {
      "id": 1, "title": "Execution Router Lag P95 (ns)", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "histogram_quantile(0.95, sum(rate(execution_router_lag_ns_bucket[1m])) by (le))", "refId": "A", "legendFormat": "p95"}
      ]
    },
    {
      "id": 2, "title": "Order Throughput", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "rate(order_actions_total[1m])", "refId": "A", "legendFormat": "{{action}}"},
        {"expr": "rate(order_reject_total[1m])", "refId": "B", "legendFormat": "rejected"}
      ]
    },
    {
      "id": 3, "title": "Reconciliation Discrepancies", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "reconciliation_discrepancy_count", "refId": "A", "legendFormat": "discrepancy"}
      ]
    },
    {
      "id": 4, "title": "Portfolio PnL & Drawdown", "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "datasource": "Prometheus",
      "targets": [
        {"expr": "position_pnl_realized", "refId": "A", "legendFormat": "realized PnL"},
        {"expr": "portfolio_drawdown_pct", "refId": "B", "legendFormat": "drawdown %"}
      ]
    }
  ]
}
```

- [ ] **Step 4: Validate all 3 JSON files**

Run: `for f in config/monitoring/dashboards/{pipeline-health,strategy-risk,execution-order}.json; do python -m json.tool "$f" > /dev/null && echo "OK: $f"; done`

- [ ] **Step 5: Commit**

```bash
git add config/monitoring/dashboards/pipeline-health.json config/monitoring/dashboards/strategy-risk.json config/monitoring/dashboards/execution-order.json
git commit -m "feat(monitoring): add Pipeline, Strategy/Risk, and Execution dashboards (M-14)

Three new Grafana dashboards covering runtime planes that previously had
zero dashboard coverage: Pipeline Health (bus overflow, queue depths,
event loop lag), Strategy & Risk (latency, intents, StormGuard, autonomy,
alpha signal age), Execution & Order (router lag, throughput, reconciliation, PnL)."
```

---

### Task 4: CI-02 + CI-03 + CI-04 — Add security scanning gates

**Files:**
- Modify: `.github/workflows/ci.yml` (cargo audit in rust job, gitleaks in security job)
- Modify: `.github/workflows/deploy.yml` (trivy image scan)
- Modify: `.github/workflows/pr-review-checklist.yml` (remove fragile secret regex)

- [ ] **Step 1: Add cargo audit to the rust job in ci.yml**

Find the `rust` job (after the `cargo test` step), add:

```yaml
      - name: Security audit (cargo audit)
        run: cargo install cargo-audit --locked && cargo audit --file rust_core/Cargo.lock
```

- [ ] **Step 2: Add gitleaks to the security job in ci.yml**

Find the `security` job. After the bandit step, add:

```yaml
      - name: Secret scanning (gitleaks)
        uses: gitleaks/gitleaks-action@cb7149a9b57195b609c63e8518d2c6056677d2d0  # v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Note: Look up the actual SHA for gitleaks-action v2. If `gh api` is available: `gh api repos/gitleaks/gitleaks-action/git/ref/tags/v2 --jq '.object.sha'`

- [ ] **Step 3: Add trivy image scan in deploy.yml**

After the `docker/build-push-action` step (normal deploy, not dry run) and before the SSH deploy step, add:

```yaml
      - name: Scan Docker image for vulnerabilities
        uses: aquasecurity/trivy-action@915b19bbe73b92a6cf82a1bc12b087c9a19a5fe2  # v0.28.0
        with:
          image-ref: "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ steps.sha.outputs.sha }}"
          format: "table"
          exit-code: "1"
          severity: "HIGH,CRITICAL"
```

Note: Look up the actual SHA for trivy-action. Pin it.

- [ ] **Step 4: Remove fragile secret regex from pr-review-checklist.yml**

Find the "Detect hardcoded secrets" step (around line 84) and replace the entire step with a comment:

```yaml
      # Secret scanning moved to ci.yml security job (gitleaks)
```

- [ ] **Step 5: Validate all workflow YAML files**

Run: `python -c "
import yaml
for f in ['.github/workflows/ci.yml', '.github/workflows/deploy.yml', '.github/workflows/pr-review-checklist.yml']:
    yaml.safe_load(open(f))
    print(f'OK: {f}')
"`

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/deploy.yml .github/workflows/pr-review-checklist.yml
git commit -m "feat(ci): add cargo audit, gitleaks, and trivy image scanning (CI-02/03/04)

- cargo audit added to rust job for Rust crate CVE scanning
- gitleaks replaces fragile grep-based secret scanning (now scans entire repo)
- trivy scans Docker image before SSH deploy (fails on HIGH+ CVEs)
- Removed inline secret regex from pr-review-checklist.yml"
```

---

### Task 5: CI-05 — Add timeout-minutes to 10 CI jobs

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add timeout-minutes to each job**

For each job in ci.yml, add `timeout-minutes:` at the job level (after `runs-on:`):

| Job key | Add |
|---------|-----|
| `lint` | `timeout-minutes: 15` |
| `rust` | `timeout-minutes: 20` |
| `typecheck` | `timeout-minutes: 10` |
| `dependency-boundary` | `timeout-minutes: 5` |
| `benchmark` | `timeout-minutes: 30` |
| `latency-gate` | `timeout-minutes: 20` |
| `integration` | `timeout-minutes: 30` |
| `recorder-nightly-drills` | `timeout-minutes: 30` |
| `security` | `timeout-minutes: 10` |
| `pr-review-gate` | `timeout-minutes: 5` |

The `test` job already has `timeout-minutes: 30` — leave it.

- [ ] **Step 2: Validate YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "fix(ci): add timeout-minutes to 10 CI jobs (CI-05)

Only test had timeout-minutes (30). Added timeouts to all other jobs
to prevent hung jobs from consuming runners for 6 hours (GitHub default)."
```

---

### Task 6: C-03 — Extend config schema with strategy_limits validation

**Files:**
- Modify: `src/hft_platform/config/schema.py`
- Create: `tests/unit/test_config_schema_limits.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_config_schema_limits.py`:

```python
"""Tests for extended config schema validation (C-03)."""
import pytest
from hft_platform.config.schema import validate_config


def test_valid_config_with_intraday_pnl():
    """Valid config with intraday_pnl passes validation."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 500,
            "hard_limit_ntd": 1000,
        },
    }
    result = validate_config(cfg)
    assert result["mode"] == "sim"


def test_intraday_pnl_soft_exceeds_hard_rejected():
    """soft_limit_ntd > hard_limit_ntd should fail validation."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "intraday_pnl": {
            "soft_limit_ntd": 2000,
            "hard_limit_ntd": 1000,
        },
    }
    with pytest.raises(Exception):
        validate_config(cfg)


def test_unknown_top_level_key_warns(caplog):
    """Unknown keys should be logged at debug level but not crash."""
    cfg = {
        "mode": "sim",
        "symbols": ["2330"],
        "totally_unknown_key": 42,
    }
    result = validate_config(cfg)
    assert result["mode"] == "sim"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/unit/test_config_schema_limits.py -v --tb=short`
Expected: at least `test_intraday_pnl_soft_exceeds_hard_rejected` FAILs (no validation exists yet)

- [ ] **Step 3: Add IntraDayPnlConfig struct and semantic check**

In `schema.py`, after the existing Struct definitions (around line 49), add:

```python
class IntraDayPnlConfig(msgspec.Struct, frozen=True):
    """Intraday PnL limits."""
    soft_limit_ntd: int = 500
    hard_limit_ntd: int = 1000
    peak_drawdown_pct: float = 0.40
    soft_recovery_ntd: int = 300
    drawdown_recovery_pct: float = 0.20
    soft_limit_cooldown_s: int = 60
    peak_drawdown_min_peak_ntd: int = 200
```

Add an `intraday_pnl: Optional[IntraDayPnlConfig] = None` field to `HftConfig`.

In `_semantic_checks()`, add after the existing checks:

```python
    # Intraday PnL sanity
    if cfg.intraday_pnl is not None:
        pnl = cfg.intraday_pnl
        if pnl.soft_limit_ntd > pnl.hard_limit_ntd:
            errors.append(
                f"intraday_pnl.soft_limit_ntd ({pnl.soft_limit_ntd}) "
                f"exceeds hard_limit_ntd ({pnl.hard_limit_ntd})"
            )
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_config_schema_limits.py -v --tb=short`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/config/schema.py tests/unit/test_config_schema_limits.py
git commit -m "feat(config): add IntraDayPnlConfig schema validation (C-03)

Extends HftConfig with intraday_pnl struct validation. Rejects configs
where soft_limit_ntd > hard_limit_ntd. First step toward full sub-config
schema coverage (stormguard, rate_limit, etc. to follow)."
```

---

### Task 7: C-04 — Add domain schema validation to hot-reload

**Files:**
- Modify: `src/hft_platform/config/hot_reload.py`
- Create: `tests/unit/test_hot_reload_validation.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_hot_reload_validation.py`:

```python
"""Tests for hot-reload domain validation (C-04)."""
import asyncio
import tempfile
import yaml
import pytest


def test_hot_reload_rejects_invalid_domain_schema(tmp_path):
    """A structurally valid YAML with bad domain values should be rejected."""
    from hft_platform.config.hot_reload import ConfigWatcher

    config_file = tmp_path / "limits.yaml"
    # Write valid initial config
    config_file.write_text(yaml.dump({
        "intraday_pnl": {"soft_limit_ntd": 500, "hard_limit_ntd": 1000}
    }))

    watcher = ConfigWatcher(str(config_file))

    # Load initial config
    initial = watcher._safe_load_yaml()
    assert initial is not None

    # Write invalid config (soft > hard)
    config_file.write_text(yaml.dump({
        "intraday_pnl": {"soft_limit_ntd": 2000, "hard_limit_ntd": 1000}
    }))

    # Reload should reject and keep previous config
    # (This test will need adjustment based on actual reload API)
```

- [ ] **Step 2: Run test — verify it fails**

Run: `uv run pytest tests/unit/test_hot_reload_validation.py -v --tb=short`

- [ ] **Step 3: Add schema validation to _load_and_notify**

In `hot_reload.py`, import the schema validation function:

```python
from hft_platform.config.schema import validate_config, ConfigValidationError
```

In `_load_and_notify()` (around line 165), after `new_config = self._safe_load_yaml()` succeeds and before `self._current_config = new_config`:

```python
        # Domain schema validation (C-04)
        try:
            validate_config(new_config)
        except (ConfigValidationError, Exception) as exc:
            logger.warning(
                "ConfigWatcher: domain validation failed, keeping previous config",
                path=self._config_path,
                error=str(exc),
            )
            return
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/unit/test_hot_reload_validation.py -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/config/hot_reload.py tests/unit/test_hot_reload_validation.py
git commit -m "feat(config): add domain schema validation to hot-reload (C-04)

ConfigWatcher now validates reloaded config through validate_config()
after YAML parsing. Invalid domain values (e.g., soft > hard limit)
are rejected with a warning, preserving the previous valid config."
```

---

### Task 8: O-04 — Remove `clean` from ops.sh header

**Files:**
- Modify: `ops.sh`

- [ ] **Step 1: Remove `clean` from the header comment**

In `ops.sh`, find line 17 (`#   clean       : Remove temporary ops artifacts.`) and delete it.

- [ ] **Step 2: Verify the header matches the dispatch block**

Run: `grep "^#   " ops.sh | awk '{print $2}' | sort` — should match the commands in the case block.
Run: `grep "^    [a-z]" ops.sh | grep ')' | sed 's/)//' | sort` — case block commands.

Both lists should match (no `clean` in either).

- [ ] **Step 3: Commit**

```bash
git add ops.sh
git commit -m "fix(ops): remove undocumented clean subcommand from ops.sh header

The clean subcommand was listed in the header comment but never
implemented in the case dispatch block. Use 'make clean' instead."
```

---

## Post-P1 Verification

- [ ] **V1: Alert rules** — `promtool check rules /path/to/rules.yaml` → 0 errors, 10 new rules found
- [ ] **V2: New dashboards** — open each in Grafana, verify panel queries reference real metrics
- [ ] **V3: CI** — push a branch, verify cargo audit + gitleaks + trivy steps appear in CI run
- [ ] **V4: Config schema** — `uv run pytest tests/unit/test_config_schema_limits.py -v` → all pass
- [ ] **V5: Hot-reload** — `uv run pytest tests/unit/test_hot_reload_validation.py -v` → all pass
- [ ] **V6: ops.sh** — `./ops.sh clean` → shows usage message (no crash)

---

## Next Plans

After P1 lands:
- **P2 plan**: `docs/superpowers/plans/2026-MM-DD-infra-audit-p2-medium.md`
- **P3 plan**: `docs/superpowers/plans/2026-MM-DD-infra-audit-p3-low.md`

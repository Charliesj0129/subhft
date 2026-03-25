# Quarterly Chaos Drill Runbook

## Purpose

Validate system resilience quarterly before production confidence decays. Chaos drills systematically exercise failure modes to ensure the platform degrades gracefully, recovers automatically, and never silently loses data or places unintended orders.

## Pre-Drill Checklist

- [ ] Confirm system is in sim mode (`HFT_MODE=sim`) — never run drills against live trading
- [ ] Back up ClickHouse data (`./scripts/clickhouse_backup.sh` or `./scripts/daily-backup.sh`)
- [ ] Notify team of drill window (post in ops channel with expected duration ~30 min)
- [ ] Verify all services running: `docker compose ps` — all should show `Up (healthy)`
- [ ] Record baseline metrics: snapshot Prometheus dashboards or run `curl localhost:9090/metrics > /tmp/pre-drill-metrics.txt`

## Execution

### Automated Mode (Recommended)

Run the full drill suite with a single command:

```bash
./scripts/run-chaos-drill.sh
```

Output is printed to console and saved to `/tmp/chaos-drill-YYYYMMDD.log`.

### Manual Mode

Run each playbook individually for deeper investigation:

```bash
uv run pytest tests/chaos/test_playbook_broker_disconnect.py -v --no-cov
uv run pytest tests/chaos/test_playbook_clickhouse_down.py -v --no-cov
uv run pytest tests/chaos/test_playbook_feed_gap.py -v --no-cov
uv run pytest tests/chaos/test_playbook_position_drift.py -v --no-cov
uv run pytest tests/chaos/test_playbook_disk_full.py -v --no-cov
```

## Playbook Details

### 1. broker_disconnect

- **Failure mode**: Broker WebSocket connection drops mid-session (network partition, broker maintenance).
- **Expected system response**: StormGuard transitions to HALT, pending orders are cancelled, reconnect loop activates with exponential backoff.
- **Pass criteria**: System enters HALT within `HFT_STORMGUARD_FEED_GAP_HALT_S` seconds, no new orders placed during disconnect, reconnect succeeds and trading resumes.
- **If it fails**: Check `feed_adapter/` reconnect logic, verify `HFT_RECONNECT_BACKOFF_S` and `HFT_RECONNECT_BACKOFF_MAX_S` configuration, review StormGuard FSM transitions.

### 2. clickhouse_down

- **Failure mode**: ClickHouse becomes unreachable (crash, network issue, disk full).
- **Expected system response**: Recorder falls back to WAL-only mode, trading continues uninterrupted, WAL files accumulate in `.wal/` directory.
- **Pass criteria**: No data loss (WAL captures all events), trading hot path latency unaffected, recorder emits `recorder_fallback_total` metric.
- **If it fails**: Check `recorder/wal.py` fallback path, verify `.wal/` directory is writable, review `HFT_RECORDER_MODE` setting.

### 3. feed_gap

- **Failure mode**: Market data feed goes silent (no ticks for extended period).
- **Expected system response**: Quote staleness detection triggers, StormGuard transitions to HALT after `HFT_STORMGUARD_FEED_GAP_HALT_S` seconds, flap detection prevents rapid re-subscribe cycling.
- **Pass criteria**: HALT triggered within configured threshold, `feed_gap_detected_total` metric incremented, system recovers when feed resumes.
- **If it fails**: Check `HFT_STORMGUARD_FEED_GAP_HALT_S` threshold, verify quote staleness monitoring in `MarketDataService`, review flap detection settings (`HFT_QUOTE_FLAP_*`).

### 4. position_drift

- **Failure mode**: Local position state diverges from broker-reported positions (missed fill, duplicate fill).
- **Expected system response**: Reconciliation detects drift, emits `position_drift_detected_total` metric, logs discrepancy details for manual review.
- **Pass criteria**: Drift detected and reported within one reconciliation cycle, no silent position inaccuracy, alert fires if drift exceeds configured threshold.
- **If it fails**: Check `execution/positions.py` reconciliation logic, verify broker account query is returning accurate data, review `AccountGateway` implementation.

### 5. disk_full

- **Failure mode**: WAL directory or ClickHouse data disk reaches capacity.
- **Expected system response**: WAL writer detects disk pressure, emits `wal_disk_pressure_critical` alert, recorder enters degraded mode (drops non-critical events).
- **Pass criteria**: System does not crash, trading continues in degraded mode, disk pressure metric and alert fire correctly.
- **If it fails**: Check `recorder/wal.py` disk pressure detection, verify `HFT_WAL_RETENTION_DAYS` auto-cleanup, review disk monitoring thresholds.

## Post-Drill Checklist

- [ ] Verify all services recovered: `docker compose ps` — all healthy
- [ ] Check metrics for anomalies: compare Prometheus dashboards against pre-drill baseline
- [ ] Review drill log: `cat /tmp/chaos-drill-YYYYMMDD.log`
- [ ] Update sign-off table below

## Sign-Off Table

| Date | Operator | Pass/Fail | Duration | Notes |
|------|----------|-----------|----------|-------|
|      |          |           |          |       |

## Escalation

If any playbook fails:

1. Do not proceed with production deployment until the failure is resolved.
2. Open a GitHub issue with:
   - Title: `chaos-drill: <playbook_name> FAIL on YYYY-MM-DD`
   - Body: attach the full drill log (`/tmp/chaos-drill-YYYYMMDD.log`)
   - Label: `ops`, `resilience`
3. Assign to the on-call engineer for root cause analysis.
4. Re-run the failed playbook after the fix to confirm resolution.

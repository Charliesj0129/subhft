# Daily Operations Checklist

## Pre-Market (T-30 minutes)

### Infrastructure Health

- [ ] **ClickHouse health**:
  ```bash
  docker exec clickhouse clickhouse-client --query "SELECT 1"
  # Must return: 1
  ```
- [ ] **ClickHouse disk usage**:
  ```bash
  docker exec clickhouse clickhouse-client \
    --query "SELECT formatReadableSize(free_space), formatReadableSize(total_space) FROM system.disks"
  # free_space should be > 20% of total_space
  ```
- [ ] **WAL disk usage**:
  ```bash
  du -sh .wal/
  # Alert if > 1GB (indicates CH write failures accumulating)
  ```
- [ ] **Redis health** (if monitor enabled):
  ```bash
  redis-cli -h ${HFT_MONITOR_REDIS_HOST:-localhost} ping
  # Must return: PONG
  ```

### Broker Connectivity

- [ ] **API credentials valid**:
  ```bash
  uv run hft admin check-credentials
  ```
- [ ] **Market calendar**: Confirm today is a trading day (check TWSE calendar for holidays).
- [ ] **Contract updates**: Verify futures contract expiry dates are current.
  ```bash
  uv run hft admin check-contracts
  ```

### Configuration

- [ ] **Config review**: No uncommitted config changes.
  ```bash
  git diff config/
  ```
- [ ] **Symbol list**: `HFT_SYMBOLS` or `symbols.yaml` matches intended universe.
- [ ] **Risk limits**: Verify daily PnL limits, position limits, and order rate limits.

### Service Startup

- [ ] **Start services**:
  ```bash
  docker compose up -d
  docker compose ps  # All services should show "Up (healthy)"
  ```
- [ ] **Verify metrics endpoint**:
  ```bash
  curl -s http://localhost:9090/metrics | head -5
  ```

---

## During Market Hours

### Continuous Monitoring

- [ ] **Queue depths** (every 15 min):
  ```bash
  curl -s http://localhost:9090/metrics | grep -E 'raw_queue_depth|recorder_queue_depth|risk_queue_depth'
  # All should be < 80% of capacity
  ```
- [ ] **Circuit breakers**: Check Grafana dashboard for any triggered circuit breakers.
- [ ] **Feed rate**:
  ```bash
  curl -s http://localhost:9090/metrics | grep hft_feed_ticks_total
  # Should be incrementing steadily during market hours
  ```
- [ ] **StormGuard state**:
  ```bash
  curl -s http://localhost:9090/metrics | grep hft_storm_guard_state
  # Must be: state="NORMAL"
  ```
- [ ] **Error rate**: Check structlog for ERROR/CRITICAL entries.
  ```bash
  docker compose logs hft-engine --since 15m 2>&1 | grep -c '"level":"error"'
  # Should be 0 or near-0
  ```

### Alerts

- [ ] Acknowledge any PagerDuty/Slack alerts promptly.
- [ ] If HALT triggered, follow `halt-recovery.md` runbook.
- [ ] If `STRATEGY_QUARANTINED` alert fires, isolate the strategy for the rest of the session unless postmortem evidence supports a manual re-arm.
- [ ] If `PLATFORM_REDUCE_ONLY` alert fires, stop expecting new exposure; verify flatten-only behavior and plan a manual re-arm after reconciliation.

---

## Post-Market (T+15 minutes)

### Reconciliation

- [ ] **Position reconciliation**:
  ```bash
  uv run hft admin reconcile --verbose
  # Must show zero discrepancies
  ```
- [ ] **Fill reconciliation**: Compare internal fill count vs broker-reported fills.
- [ ] **Autonomy state**:
  ```bash
  hft ops autonomy-status
  ```
  No pending manual re-arm should remain unless the next session is intentionally blocked.

### Data Flush

- [ ] **Flush recorder buffers**:
  ```bash
  # Recorder auto-flushes on shutdown, but verify:
  docker compose logs hft-engine --since 5m 2>&1 | grep 'recorder.*flush'
  ```
- [ ] **WAL replay** (if any WAL files accumulated):
  ```bash
  ls -la .wal/*.wal 2>/dev/null
  # If files exist:
  docker compose run --rm wal-loader
  ```

### PnL Summary

- [ ] **Daily PnL report**:
  ```sql
  SELECT
      strategy,
      sum(realized_pnl) / 10000.0 AS realized_pnl,
      count() AS trade_count
  FROM hft.fills
  WHERE toDate(ts / 1e9) = today()
  GROUP BY strategy
  ORDER BY realized_pnl DESC;
  ```
- [ ] **Compare with broker statement** when available.

### Archive and Cleanup

- [ ] **WAL archive**:
  ```bash
  # Old WAL files are auto-cleaned based on HFT_WAL_RETENTION_DAYS
  # Verify no stale files:
  find .wal/ -name "*.wal" -mtime +7 | head -5
  ```
- [ ] **Log rotation**: Verify Docker log rotation is active.
- [ ] **Stop services** (if not running overnight):
  ```bash
  docker compose down
  ```
- [ ] **Evidence pack**:
  Verify the autonomy evidence dir contains `state_timeline.jsonl`,
  `platform_degrade.json`, `strategy_quarantine.json`, and
  `manual_rearm_requirements.md`.

  **Read it inside the container, not on the host.** `outputs/` is *not*
  bind-mounted, and the engine's CWD is `/app`, so the live state lives in the
  container's writable layer:
  ```bash
  docker compose exec hft-engine ls /app/outputs/production_rollout/autonomy/$(date +%Y%m%d)/
  docker compose exec hft-engine cat /app/outputs/production_rollout/autonomy/runtime_state.json
  ```
  A host-side `outputs/production_rollout/autonomy/runtime_state.json` is *not*
  engine state — anything run from the deployment root with a relative default
  path (a stray `pytest`, or `hft ops autonomy-status` on the host) writes there.
  On 2026-07-08 a host test run left `reason="test_reason"` plus phantom
  `strat1`/`strat_a` latches in `/home/charl/subhft`, which then showed up as
  real operator-facing state. Treat any such file as an artifact and move it aside.

  Because the evidence lives in the writable layer, `docker compose up -d`
  / `--force-recreate` **destroys it**. Rescue first:
  ```bash
  docker cp hft-engine:/app/outputs/production_rollout/autonomy ./outputs/autonomy_container_$(date +%Y%m%d)
  ```

---

## Weekly Tasks

- [ ] Review ClickHouse TTL cleanup (`system.mutations` for pending TTL merges).
- [ ] Review Grafana dashboard for multi-day trends.
- [ ] Update contract expiry dates for next week.
- [ ] Rotate API credentials if policy requires.

---

## Weekend → Monday Reopen Checklist

Run over the weekend, before the first session of the week. A latched autonomy
state or a WAL condition left over from Friday will not clear on its own.

### Saturday/Sunday — clear anything latched

- [ ] **No critical alerts other than known weekend false positives**:
  ```bash
  curl -s localhost:9093/api/v2/alerts | python3 -c 'import sys,json;[print(a["labels"]["alertname"], a["status"]["state"]) for a in json.load(sys.stdin)]'
  ```
  `ShioajiPendingQuoteStall` fires every weekend and clears at Monday's open —
  `shioaji_quote_pending_age_seconds` simply counts up from the last night-session
  close, and the rule has no session/holiday awareness. Anything else is real.
- [ ] **Autonomy not latched**:
  ```bash
  curl -s localhost:9090/metrics | grep -E 'autonomy_mode|manual_rearm_required|platform_reduce_only_active'
  ```
  Expect `autonomy_mode{scope="platform"} 0.0` and `manual_rearm_required 0.0`.
  Non-zero means a latch survives; `recorder_data_loss` in particular is
  non-auto-recoverable and re-latches across restarts — follow
  `WALDiskPressureCritical.md`.
- [ ] **WAL root clean** (top-level files are what the pressure monitor counts):
  ```bash
  find .wal -maxdepth 1 -type f -printf '%s\t%p\n' | sort -rn | head
  ls .wal/*.jsonl 2>/dev/null | wc -l    # pending backlog, expect 0 when idle
  curl -s localhost:9090/metrics | grep -E 'disk_pressure_level|wal_disk_available_mb'
  ```
- [ ] **No dropped rows last session**:
  ```bash
  docker compose logs hft-engine --since 72h | grep -i 'WALFirstWriter HALT'
  ```
- [ ] **11/11 containers healthy**: `docker compose ps`
- [ ] Session-refresh errors reviewed — repeated `code: 451, detail: Too Many
  Connections` means quote facades are re-logging in unserialized; note it, it
  degrades reconnects rather than blocking the open.

### Monday pre-open

- [ ] 08:30 CST — `shioaji_quote_pending_age_seconds` drops to ~0 as sessions refresh.
- [ ] 09:00 CST — ingestion live; compare the running row rate against a recent
  healthy day (~6.5–7.2 M rows/day at a 296-symbol universe):
  ```bash
  docker exec clickhouse clickhouse-client --query "SELECT count(), uniqExact(symbol) FROM hft.market_data WHERE toDate(fromUnixTimestamp64Nano(ingest_ts),'Asia/Taipei') = today()"
  ```
- [ ] First 30 minutes — no `WALFirstWriter HALT`, no new criticals, event-loop lag
  within budget.

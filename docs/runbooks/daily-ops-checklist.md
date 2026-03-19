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

---

## Post-Market (T+15 minutes)

### Reconciliation

- [ ] **Position reconciliation**:
  ```bash
  uv run hft admin reconcile --verbose
  # Must show zero discrepancies
  ```
- [ ] **Fill reconciliation**: Compare internal fill count vs broker-reported fills.

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

---

## Weekly Tasks

- [ ] Review ClickHouse TTL cleanup (`system.mutations` for pending TTL merges).
- [ ] Review Grafana dashboard for multi-day trends.
- [ ] Update contract expiry dates for next week.
- [ ] Rotate API credentials if policy requires.

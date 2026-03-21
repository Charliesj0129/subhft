# Runbook: ClickHouse Down

## Trigger

- Prometheus alert `ClickHouseConnectionDown` fires.
- `hft-monitor` logs report ClickHouse health check failure.
- WAL files accumulate in `.wal/` directory (recorder falls back to WAL-first mode).
- Engine logs show ClickHouse insert errors or connection refused.

## Impact

- **Recording path degrades**: When `HFT_RECORDER_MODE=wal_first` (default for engine), data is written to WAL files on local disk. No data loss if WAL is healthy.
- **Recording path fails**: When `HFT_RECORDER_MODE=direct`, inserts fail and data may be dropped (recorder uses `put_nowait` with drop policy).
- **Trading continues**: ClickHouse outage does NOT block the hot path. Strategies, risk, and order execution are unaffected.
- **Monitor dashboard stale**: Grafana/monitor panels that query ClickHouse show stale data.
- **Disk pressure risk**: Prolonged outage causes WAL files to accumulate, risking disk exhaustion. WAL disk circuit breaker activates at `HFT_WAL_DISK_MIN_MB` (default 500 MB).

## Diagnosis

### 1. Check ClickHouse container status

```bash
docker compose ps clickhouse
docker compose logs --tail=50 clickhouse
```

### 2. Check ClickHouse health endpoint

```bash
# From host (ports bound to 127.0.0.1)
wget --no-verbose --tries=1 --spider http://127.0.0.1:8123/ping 2>&1

# From inside a service container
docker exec hft-engine wget --no-verbose --tries=1 --spider http://clickhouse:8123/ping 2>&1
```

### 3. Check for OOM kill or resource exhaustion

```bash
# Docker events for OOM
docker events --filter container=clickhouse --since 1h 2>/dev/null | grep -i oom

# Check memory limit (configured at 6G)
docker stats clickhouse --no-stream

# Check disk usage on ClickHouse volumes
docker exec clickhouse du -sh /var/lib/clickhouse/data/hot /var/lib/clickhouse/data/cold 2>/dev/null
```

### 4. Check WAL accumulation

```bash
# WAL backlog
ls -la .wal/*.jsonl 2>/dev/null | wc -l
du -sh .wal/

# WAL disk circuit breaker status
curl -s http://localhost:9090/metrics | grep -E "wal_disk_available_mb|wal_disk_circuit_breaker"
```

## Resolution

### Step 1: Restart ClickHouse

```bash
docker compose restart clickhouse

# Wait for health check to pass
docker compose ps clickhouse
# Should show "Up (healthy)" within ~90s
```

### Step 2: Verify ClickHouse is accepting queries

```bash
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT 1"
```

### Step 3: Drain WAL backlog

The `wal-loader` service automatically picks up and replays WAL files:

```bash
# Check wal-loader is running
docker compose ps wal-loader

# If not running, start it
docker compose up -d wal-loader

# Monitor WAL drain progress
docker compose logs -f wal-loader | head -50

# Check status
make recorder-status
```

### Step 4: If ClickHouse won't start (data corruption)

```bash
# Check ClickHouse error logs
docker compose logs --tail=200 clickhouse | grep -iE "error|corrupt|exception"

# If metadata is corrupted, try removing problematic parts
# WARNING: This may require manual data recovery
docker compose stop clickhouse

# Option A: Reset metadata volume (loses table definitions, not data files)
# docker volume rm hft_platform_ch_metadata
# Then recreate tables via migrations

# Option B: Start with recovery mode
docker compose up -d clickhouse
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "SYSTEM RESTART REPLICA hft.market_data"
```

### Step 5: If disk is full

```bash
# Check which volume is full
docker exec clickhouse df -h /var/lib/clickhouse

# Apply TTL cleanup if not already configured
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "OPTIMIZE TABLE hft.market_data FINAL"

# Emergency: drop system log tables that grow unbounded
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "TRUNCATE TABLE IF EXISTS system.trace_log"
```

## Rollback

If ClickHouse restart causes issues:

```bash
# Stop ClickHouse and let the engine run in WAL-only mode
docker compose stop clickhouse

# Engine continues writing to WAL; data is preserved
# Fix ClickHouse separately and restart when ready
```

## Post-Incident

1. **Verify data integrity after WAL replay**:
   ```bash
   docker exec clickhouse clickhouse-client \
     --password "${CLICKHOUSE_PASSWORD}" \
     --query "SELECT toStartOfHour(toDateTime64(exch_ts/1e9, 3)) AS hour, count()
              FROM hft.market_data
              WHERE toDate(exch_ts/1e9) = today()
              GROUP BY hour ORDER BY hour"
   ```

2. **Check for DLQ files** (failed inserts):
   ```bash
   ls -la .wal/dlq/ 2>/dev/null
   # If present, replay them
   make wal-dlq-replay-dry-run
   make wal-dlq-replay
   ```

3. **Check WAL archive disk usage**:
   ```bash
   du -sh .wal/archive/
   # Clean old archives if needed
   make wal-archive-cleanup WAL_KEEP_DAYS=7
   ```

4. **Review ClickHouse memory config** (`config/clickhouse_memory.xml`) if OOM was the cause.

5. **Run drill to validate WAL fallback works correctly**:
   ```bash
   make drill-ck-down
   ```

6. **File incident report** if outage lasted > 5 minutes during trading hours.

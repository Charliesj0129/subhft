# Runbook: WAL Replay

## Trigger

- ClickHouse was down and has recovered; WAL files accumulated in `.wal/`.
- `make recorder-status` shows WAL backlog > 0.
- DLQ files exist in `.wal/dlq/` from previously failed inserts.
- Post-market recovery: replay data recorded during session into ClickHouse.

## Impact

- **Data gap in ClickHouse**: Queries, dashboards, and research pipelines see missing data until WAL is replayed.
- **Disk usage**: WAL files consume local disk. Prolonged accumulation risks circuit breaker activation at `HFT_WAL_DISK_MIN_MB` (default 500 MB).
- **No trading impact**: WAL replay is a background persistence operation; it does not affect the hot path.

## Diagnosis

### 1. Assess WAL backlog

```bash
# Count pending WAL files
ls .wal/*.jsonl 2>/dev/null | wc -l

# Total WAL size
du -sh .wal/

# Check for batch WAL files (coalesced multi-table format)
ls .wal/batch_*.jsonl 2>/dev/null | wc -l

# Check for single-table WAL files
ls .wal/market_data_*.jsonl .wal/orders_*.jsonl .wal/fills_*.jsonl 2>/dev/null | wc -l
```

### 2. Check DLQ (Dead Letter Queue)

```bash
# DLQ file count and size
make wal-dlq-status

# Or manually
ls -la .wal/dlq/ 2>/dev/null
du -sh .wal/dlq/ 2>/dev/null
```

### 3. Check for corrupt WAL files

```bash
ls -la .wal/corrupt/ 2>/dev/null
```

### 4. Verify ClickHouse is healthy

```bash
docker compose ps clickhouse
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT 1"
```

## Resolution

### Standard WAL replay (via wal-loader service)

The `wal-loader` service is the primary replay mechanism. It polls `.wal/` for `.jsonl` files and inserts into ClickHouse with retry and dedup support.

```bash
# Start wal-loader if not running
docker compose up -d wal-loader

# Monitor replay progress
docker compose logs -f wal-loader

# Check status
make recorder-status
```

Configuration tuning for large backlogs:

```bash
# Increase loader concurrency for faster drain
HFT_WAL_LOADER_CONCURRENCY=4 docker compose up -d wal-loader

# Increase poll frequency
HFT_WAL_POLL_INTERVAL_S=0.5 docker compose up -d wal-loader
```

### DLQ replay (previously failed inserts)

```bash
# Dry run first — see what would be replayed
make wal-dlq-replay-dry-run

# Execute replay
make wal-dlq-replay

# Limit to N files at a time (for large DLQ)
make wal-dlq-replay MAX_FILES=100
```

### Manual WAL replay (one-shot)

If the wal-loader service is not suitable:

```bash
docker compose run --rm wal-loader
```

### WAL file format reference

**Single-table files**: `{table}_{timestamp_ns}.jsonl` — one JSON row per line.

**Batch files** (coalesced, `WALBatchWriter`): `batch_{timestamp_ns}.jsonl` — multi-table format with header lines:
```json
{"__wal_table__":"market_data","__row_count__":150}
{"exch_ts":1234567890000000000,"price":1001000,...}
...
{"__wal_table__":"orders","__row_count__":5}
{"order_id":"abc123",...}
```

The loader handles both formats transparently.

### Handling corrupt files

```bash
# Corrupt files are quarantined in .wal/corrupt/
ls -la .wal/corrupt/ 2>/dev/null

# Inspect a corrupt file
head -5 .wal/corrupt/<filename>

# If recoverable, move back to .wal/ for retry
# mv .wal/corrupt/<filename> .wal/

# If unrecoverable, archive or delete
# rm .wal/corrupt/<filename>
```

### Insert retry configuration

The loader retries failed inserts with exponential backoff:

| Variable | Default | Purpose |
|---|---|---|
| `HFT_INSERT_MAX_RETRIES` | 4 | Max retry attempts per insert |
| `HFT_INSERT_BASE_DELAY_S` | 0.6 | Initial retry delay |
| `HFT_INSERT_MAX_BACKOFF_S` | 8.0 | Maximum retry backoff |
| `HFT_CONNECT_MAX_RETRIES` | 10 | Max ClickHouse connection retries |
| `HFT_CONNECT_BASE_DELAY_S` | 5.0 | Initial connection retry delay |
| `HFT_CONNECT_MAX_BACKOFF_S` | 300.0 | Max connection retry backoff (5 min) |

## Rollback

WAL replay is idempotent (dedup-aware). If replay inserts bad data:

```bash
# Identify the problematic time range
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT min(toDateTime64(exch_ts/1e9, 3)), max(toDateTime64(exch_ts/1e9, 3))
           FROM hft.market_data
           WHERE toDate(exch_ts/1e9) = today()"

# Delete specific rows if needed (use with caution)
docker exec clickhouse clickhouse-client \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "ALTER TABLE hft.market_data DELETE
           WHERE exch_ts BETWEEN <start_ns> AND <end_ns>"
```

## Post-Incident

1. **Verify data completeness**:
   ```bash
   docker exec clickhouse clickhouse-client \
     --password "${CLICKHOUSE_PASSWORD}" \
     --query "SELECT toStartOfMinute(toDateTime64(exch_ts/1e9, 3)) AS minute, count()
              FROM hft.market_data
              WHERE toDate(exch_ts/1e9) = today()
              GROUP BY minute ORDER BY minute"
   ```

2. **Clean WAL archive**:
   ```bash
   make wal-archive-cleanup WAL_KEEP_DAYS=7
   ```

3. **Clean orphan tmp files** (from interrupted writes):
   ```bash
   make wal-manifest-tmp-cleanup
   ```

4. **Check disk space recovered**:
   ```bash
   du -sh .wal/
   df -h .
   ```

5. **Validate recording pipeline is healthy**:
   ```bash
   make recorder-status
   curl -s http://localhost:9090/metrics | grep -E "recorder_rows_total|recorder_wal"
   ```

6. **Run WAL pressure drill** to confirm circuit breaker works:
   ```bash
   make drill-wal-pressure
   ```

# Runbook: ClickHouse / MV Pressure Tuning

## Goal

Reduce repeated `Code: 241 (MEMORY_LIMIT_EXCEEDED)` during `INSERT INTO hft.market_data` while keeping ingest freshness and replay safety.

## Scope

- Compose-level tuning only (reproducible via Git).
- No destructive SQL.
- WAL fallback path stays enabled.

## Baseline Queries

```bash
docker exec clickhouse clickhouse-client --query "
SELECT count() AS ex241_30m
FROM system.query_log
WHERE event_time > now() - INTERVAL 30 MINUTE
  AND exception_code = 241
FORMAT Vertical"

docker exec clickhouse clickhouse-client --query "
SELECT toStartOfFiveMinutes(event_time) AS t5, count() AS ex241
FROM system.query_log
WHERE event_time > now() - INTERVAL 30 MINUTE
  AND exception_code = 241
GROUP BY t5
ORDER BY t5 DESC
LIMIT 8
FORMAT Vertical"

docker exec clickhouse clickhouse-client --query "
SELECT initial_address, count() AS n, round(avg(query_duration_ms), 2) AS avg_ms,
       quantileExact(0.95)(query_duration_ms) AS p95_ms
FROM system.query_log
WHERE event_time > now() - INTERVAL 10 MINUTE
  AND query LIKE 'INSERT INTO hft.market_data%'
  AND type='QueryFinish'
GROUP BY initial_address
ORDER BY n DESC
FORMAT Vertical"
```

## Tunings Applied

1. `HFT_CH_INSERT_POOL_SIZE=4`
2. `HFT_CH_MAX_CONCURRENT_INSERTS=2`
3. `HFT_CH_INSERT_CHUNK_ROWS=256`
4. ClickHouse container memory limit: `4G -> 6G`
5. ClickHouse server cap: `max_server_memory_usage=5.2G`
6. Redis persistence disabled for session-only use:
   - `--save "" --appendonly no`
7. Engine default recorder mode switched to WAL-first:
   - `HFT_RECORDER_MODE=wal_first` (via compose command)
8. WAL replay pressure guard (wal-loader):
   - `HFT_WAL_LOADER_CONCURRENCY=1`
   - `HFT_INSERT_MAX_RETRIES=4`
   - `HFT_INSERT_BASE_DELAY_S=0.6`
   - `HFT_INSERT_MAX_BACKOFF_S=8.0`
   - Place these in shared compose env (`x-hft-common`) to avoid overriding `HFT_CLICKHOUSE_PORT=8123`.

## Deploy Steps

```bash
docker compose pull redis
docker compose up -d --force-recreate redis clickhouse hft-engine wal-loader hft-monitor
```

## Validation Gate

- `redis` is `running` with `restart_count=0`.
- `ex241_5m` trends down and ideally reaches `0`.
- `hft-engine` log has no:
  - `concurrent queries within the same session`
  - `session_lease_refresh_failed`
- New errors (if any) should shift from `hft-engine` to occasional `wal-loader` retry only.
- `market_data` still fresh:

```bash
docker exec clickhouse clickhouse-client --query "
SELECT max(ingest_ts) AS max_ingest_ns, count() AS rows_5m
FROM hft.market_data
WHERE ingest_ts > toUnixTimestamp64Nano(now64()) - 300000000000
FORMAT Vertical"
```

Optional source attribution check:

```bash
docker exec clickhouse clickhouse-client --query "
SELECT initial_address, count() AS n
FROM system.query_log
WHERE event_time > now() - INTERVAL 5 MINUTE
  AND query LIKE 'INSERT INTO hft.market_data%'
  AND type='QueryFinish'
GROUP BY initial_address
ORDER BY n DESC
FORMAT Vertical"
```

## Rollback

1. Revert tuning commit in Git and push.
2. Remote:

```bash
git pull --ff-only
docker compose up -d --force-recreate redis clickhouse hft-engine wal-loader hft-monitor
```

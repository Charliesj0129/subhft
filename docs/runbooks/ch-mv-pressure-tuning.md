# Runbook: ClickHouse / MV Pressure Tuning

## Goal

Reduce repeated `Code: 241 (MEMORY_LIMIT_EXCEEDED)` during `INSERT INTO hft.market_data` while keeping ingest freshness and replay safety.

## Scope

- Compose-level tuning only (reproducible via Git).
- No destructive SQL.
- WAL fallback path stays enabled.

## Baseline Queries

建議先以 guard wrapper 檢查（可阻擋高風險 full-scan）：

```bash
make ch-query-guard-check QUERY='SELECT count() AS ex241_30m FROM system.query_log WHERE event_time > now() - INTERVAL 30 MINUTE AND exception_code = 241'
# 例行基線（批次查詢 + 稽核報告）
make ch-query-guard-suite
```

必要時再執行：

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

## Insert SLO Guardrail (2026-03-05)

Prometheus 指標以批次最終結果計數：
- `recorder_insert_batches_total{result="success_no_retry|success_after_retry|failed_after_retry|failed_no_client"}`

SLO 閾值：
- Insert failed ratio（24h）：`<= 0.5%`
- Insert retry ratio（24h）：`<= 5%`

Quick check（Prometheus API）：

```bash
curl -fsS --get http://localhost:9091/api/v1/query \
  --data-urlencode 'query=(sum(increase(recorder_insert_batches_total{result=~"failed_after_retry|failed_no_client"}[24h])) or vector(0)) / clamp_min((sum(increase(recorder_insert_batches_total{result=~"success_no_retry|success_after_retry|failed_after_retry|failed_no_client"}[24h])) or vector(0)), 1)'

curl -fsS --get http://localhost:9091/api/v1/query \
  --data-urlencode 'query=(sum(increase(recorder_insert_batches_total{result=~"success_after_retry|failed_after_retry"}[24h])) or vector(0)) / clamp_min((sum(increase(recorder_insert_batches_total{result=~"success_no_retry|success_after_retry|failed_after_retry|failed_no_client"}[24h])) or vector(0)), 1)'
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

## Appendix A: Incident Record (2026-03-03)

### Incident ID

`INC-CHMV-20260303-01`

### Summary

- Time window: 2026-03-03 21:16 to 21:40 (UTC+8).
- Symptom: repeated `Code: 241 (MEMORY_LIMIT_EXCEEDED)` during `INSERT INTO hft.market_data`.
- Impact: engine direct-write path produced frequent CH insert failures; replay pressure shifted to wal-loader after WAL-first cutover.

### Root Causes

1. CH/MV pressure spike: `hft.ohlcv_1m_mv` aggregation path hit memory cap during burst inserts.
2. Deployment regression at 2026-03-03 21:35:
   - wal-loader service-level `environment` override dropped shared CH env keys.
   - wal-loader used default HTTP client port path (`:9000`) and failed to connect.
3. Recovered via compose fix: move replay knobs to shared env (`x-hft-common`) and remove wal-loader env override.

### Recovery Commits

1. `66c9b7b`: engine default to `HFT_RECORDER_MODE=wal_first`.
2. `a64ef60`: wal-loader replay pressure knobs (`concurrency=1`, retry/backoff).
3. `0587402`: fix env override regression; restore CH connection config for wal-loader.

### Baseline vs Post-Fix Metrics

- Baseline (2026-03-03 21:16 UTC+8):
  - `ex241_30m = 27`
  - insert source dominant: engine `172.18.0.7` (`n=972/10m`)
  - engine had frequent `ClickHouse write failed` with code 241
- Post-fix (2026-03-03 21:40 UTC+8):
  - `ex241_10m = 1`, `ex241_5m` mostly `0` (single transient retry spike observed)
  - recent insert source dominated by wal-loader `172.18.0.5`
  - WAL backlog drained from `302 files / 9.985 MB` to `1 file / 0.031 MB`
  - latest ingest watermark reached `2026-03-03 13:39:59.184714084Z` (UTC)

### Verification Checklist (Close Criteria)

1. Config correctness:
```bash
docker inspect wal-loader --format '{{json .Config.Env}}' | rg 'HFT_CLICKHOUSE_HOST|HFT_CLICKHOUSE_PORT|HFT_WAL_LOADER_CONCURRENCY'
```
2. Pressure health:
```bash
docker exec clickhouse clickhouse-client --query "
SELECT count() AS ex241_5m
FROM system.query_log
WHERE event_time > now() - INTERVAL 5 MINUTE
  AND exception_code = 241
FORMAT Vertical"
```
3. Source attribution:
```bash
docker exec clickhouse clickhouse-client --query "
SELECT initial_address, count() AS n
FROM system.query_log
WHERE event_time > now() - INTERVAL 10 MINUTE
  AND query LIKE 'INSERT INTO hft.market_data%'
  AND type='QueryFinish'
GROUP BY initial_address
ORDER BY n DESC
FORMAT Vertical"
```
4. Backlog drain:
```bash
python3 - <<'PY'
import glob, os
files = [f for f in glob.glob('.wal/*.jsonl') if os.path.isfile(f)]
print('wal_pending_files=', len(files))
print('wal_pending_mb=', round(sum(os.path.getsize(f) for f in files)/1024/1024, 3))
PY
```

### Reuse Template

For next CH/MV incident, copy this appendix section and replace:

- `Incident ID`
- `Time window`
- `Root Causes`
- `Recovery Commits`
- `Baseline vs Post-Fix Metrics`

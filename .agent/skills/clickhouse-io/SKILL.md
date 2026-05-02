<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->
---
name: clickhouse-io
description: Use when working with the project's ClickHouse schema, recorder-facing analytics tables, operational TTL or disk issues, migrations, WAL replay, or writing ClickHouse queries tied to HFT runtime and research artifacts.
---

# ClickHouse IO

Use this skill for project-specific ClickHouse concerns: schema, recorder surfaces, operational TTLs, migrations, and HFT analytics queries.

## Schema Source of Truth

`src/hft_platform/migrations/clickhouse/` — 15 migrations, 619 lines of DDL. Auto-applied on boot.

## Tables (13+)

### Core Runtime
| Table | Partition | TTL | Purpose |
| --- | --- | --- | --- |
| `hft.market_data` | YYYYMMDD | 6 months | Tick + BidAsk (price_scaled x1M, arrays for L5) |
| `hft.orders` | YYYYMMDD | 1 year | Order lifecycle (status 0-5) |
| `hft.fills` / `hft.trades` | YYYYMMDD | 1 year | Execution fills (fee_scaled, tax_scaled, decision_price, arrival_price) |

### Materialized Views
| Table | Source | Purpose |
| --- | --- | --- |
| `hft.ohlcv_1m` (MV) | market_data | 1-min OHLCV (AggregatingMergeTree) |
| `hft.latency_stats_1m` (MV) | latency_spans | P50/P95/P99 per stage |

### Operations
| Table | TTL | Purpose |
| --- | --- | --- |
| `hft.pnl_snapshots` | 90 days | Periodic position/equity snapshots |
| `hft.shadow_orders` | 30 days | Shadow mode order logging (ReplacingMergeTree) |
| `hft.reconciliation` | 1 year | 3-way reconciliation results |
| `hft.slippage_records` | 90 days | TCA slippage decomposition |
| `hft.config_snapshots` | 1 year | Boot config audit trail |
| `hft.daily_reports` | — | Daily PnL/risk reports |
| `hft.liquidity_gate_events` | — | Liquidity gate transitions |
| `hft.wal_dedup` | — | WAL replay deduplication tracking |
| `audit.*` | 2 years | Compliance audit trail |

### Schema Design Conventions
- **Prices**: `Int64 Codec(DoubleDelta, LZ4)` scaled by 1,000,000 (x1M in CH, x10000 in Python)
- **Timestamps**: `Int64` nanoseconds (epoch), not DateTime
- **Partitioning**: YYYYMMDD for time-series
- **Ordering**: `(symbol, ts)` or `(strategy_id, symbol, ts)`
- **Compression**: DoubleDelta + LZ4 for deltas, LZ4 for arrays
- **Dedup**: ReplacingMergeTree for state tables

## Recorder Pipeline

```text
Hot Path -> recorder_queue (16384, put_nowait, drop-on-full)
  -> RecorderService (22 files)
    -> Batcher (columnar double-buffer, 10K rows or 100ms flush)
      -> DataWriter -> ClickHouse INSERT (clickhouse-connect HTTP 8123)
                    -> WAL fallback on failure

WAL-first mode (HFT_RECORDER_MODE=wal_first):
  -> WALWriter -> .wal/*.jsonl -> WALLoaderService (polling 1s)
    -> insert_with_dedup() -> ClickHouse
    -> DLQ on corrupt batch
```

## Migration Rules

1. Add new migrations in `src/hft_platform/migrations/clickhouse/` with naming: `YYYYMMDD_NNN_description.sql`
2. Never modify existing migrations (they're already applied)
3. Preserve WAL replay compatibility (additive columns OK, renames break replay)
4. Add TTL policy for any new table with runtime data
5. Test with `make test-clickhouse-writer-smoke`

## Typical Queries

### Recent fills
```sql
SELECT symbol, side, price_scaled / 10000.0 AS price, qty,
       fee_scaled / 10000.0 AS fee, tax_scaled / 10000.0 AS tax
FROM hft.fills
WHERE toDate(ts_exchange / 1e9) = today()
ORDER BY ts_exchange DESC
LIMIT 100;
```

### Daily PnL by strategy
```sql
SELECT strategy_id, symbol,
       sum(realized_pnl_scaled) / 10000.0 AS pnl_ntd,
       count() AS fills
FROM hft.pnl_snapshots
WHERE toDate(snapshot_ts / 1e9) = today()
GROUP BY strategy_id, symbol
ORDER BY pnl_ntd DESC;
```

### Latency P99
```sql
SELECT symbol, quantile(0.99)(latency_ns) / 1e6 AS p99_ms
FROM hft.latency_stats_1m
WHERE toDate(ts / 1e9) = today()
GROUP BY symbol
ORDER BY p99_ms DESC;
```

### Market data count check
```sql
SELECT count(), max(toDateTime64(exch_ts/1e9,3)) AS latest
FROM hft.market_data
WHERE toDate(exch_ts/1e9) = today();
```

### WAL replay status
```sql
SELECT table_name, count() AS batches, max(processed_at) AS last_replay
FROM hft.wal_dedup
GROUP BY table_name;
```

### Slippage analysis
```sql
SELECT symbol, side,
       avg(slippage_ticks) AS avg_slip_ticks,
       quantile(0.95)(slippage_ticks) AS p95_slip_ticks,
       avg(latency_ns) / 1e6 AS avg_lat_ms
FROM hft.slippage_records
WHERE toDate(ts / 1e9) = today()
GROUP BY symbol, side;
```

## Disk & TTL Management

- Monitor: `make recorder-status`
- System logs: control via `config/clickhouse_system_logs.xml` (trace_log, text_log can explode)
- WAL cleanup: `make wal-archive-cleanup` (default 7 days retention)
- WAL DLQ: `make wal-dlq-status`, `make wal-dlq-replay-dry-run`
- Drill: `make drill-ck-down` (30s outage, tests WAL fallback)

## Operational Commands

```bash
make recorder-status                    # WAL backlog + CH status
make wal-dlq-status                     # DLQ count/bytes/age
make wal-dlq-replay-dry-run             # Preview DLQ replay
make wal-archive-cleanup                # Clean old WAL archives
make drill-ck-down                      # ClickHouse outage drill
make test-clickhouse-writer-smoke       # Writer smoke test
make ch-query-guard-check               # Guard-check SQL (read-only + scan policy)
make ch-query-guard-suite               # Run guarded query suite
```

## Boundaries

- Use `troubleshoot-metrics` for broad runtime triage
- Use `hft-execution` for fill/position/TCA data interpretation
- Use the migration path for schema changes, never edit live tables by hand
- Query guard: `make ch-query-guard-check` enforces read-only + full-scan policy

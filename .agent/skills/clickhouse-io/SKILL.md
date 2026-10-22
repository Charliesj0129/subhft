---
name: clickhouse-io
description: "ClickHouse schema, migrations, TTL and disk, guarded queries over hft.* and audit.* tables, scale conventions. Use when writing migrations or queries or diagnosing table state. Not for recorder or WAL code (hft-recorder) or research export contracts (rules/70)."
---

# ClickHouse I/O

Schema source of truth: `src/hft_platform/migrations/clickhouse/` (Do-NOT-Edit
applied migrations; append new ones).

## Migrations

1. New file `YYYYMMDD_NNN_description.sql`; never modify an applied one.
2. Additive columns only: renames and rebuilds break WAL replay. A CREATE +
   RENAME rebuild must re-apply every later ALTER.
3. Give any new runtime table a TTL and an explicit order key; use
   ReplacingMergeTree for state tables.
4. Test with `make test-clickhouse-writer-smoke`. Applying to a production
   database is a deploy action: `.agent/rules/41-deployment.md`.

## Conventions

- Raw prices are `Int64` scaled **x1,000,000**; Python/platform is x10000;
  option strikes use a different scale again. Convert explicitly.
- Timestamps are `Int64` epoch nanoseconds, not `DateTime`.
- Partition by day (YYYYMMDD); order by `(symbol, ts)` or `(strategy_id, symbol, ts)`.
- `hft.market_data` bid/ask arrays are variable length 0-5; filter on
  `length(bids_price)`. Duplicate delivery exists on some days: dedup by rule,
  never `DISTINCT` (details in `.agent/rules/70-research-data.md`).
- Tables: runtime `hft.market_data`, `orders`, `fills`, `order_intents`,
  `pnl_snapshots`, `shadow_orders`, `reconciliation`, `slippage_records`,
  `wal_dedup`, latency and OHLCV views; compliance `audit.*`. List them with
  `SHOW TABLES FROM hft` before assuming one exists; `audit.*` often holds
  what deleted logs lost.

## Querying

Production and shared instances are read-only and guarded: run
`make ch-query-guard-check` for SQL you plan to run and `make ch-query-guard-run` /
`ch-query-guard-suite` to execute. Credentials come from `.env`; never print them.
Verify which instance (local archive vs production) you are connected to before
trusting a result.

## Disk and TTL

`make recorder-status`; system logs (`trace_log`, `text_log`) can explode, so
tune them in `config/clickhouse_system_logs.xml`; WAL cleanup `make wal-archive-cleanup`;
DLQ `make wal-dlq-status`.

## Done when

A migration applies cleanly on an empty database and keeps old WAL replayable;
a query is guard-checked, uses the right scale and depth filter, and names the
instance it ran against.

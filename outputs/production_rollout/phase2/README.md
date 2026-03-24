# Phase 2 Shadow Trading Evidence

Daily evidence packs stored in YYYYMMDD subdirectories.

Each day's pack contains:
- shadow_daily_report.json — signal count, simulated PnL, latency
- latency_summary.json — P50/P95/P99 tick-to-signal latency
- shadow_orders.parquet — raw shadow order records (optional)
- incident_notes.md — operator notes if any anomaly occurred

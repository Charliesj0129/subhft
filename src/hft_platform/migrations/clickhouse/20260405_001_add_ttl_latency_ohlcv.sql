-- Migration: 20260405_001_add_ttl_latency_ohlcv
-- Purpose: Add TTL retention policies to hft.latency_spans and hft.ohlcv_1m_state,
--          which were omitted from 20260302_001_add_ttl_policies.sql.
--
-- Risk context (2026-04-05 ops readiness review):
--   hft.latency_spans:   no TTL → diagnostic spans accumulate indefinitely;
--                        at ~100k spans/day this is ~36M rows/year growth risk
--   hft.ohlcv_1m_state:  no TTL → aggregation state table grows without bound;
--                        year-old OHLCV buckets have no research or compliance value
--
-- Retention decisions:
--   hft.latency_spans    90 days   (diagnostic artifact, same as backtest_runs)
--   hft.ohlcv_1m_state   365 days  (1 year, sufficient for annual seasonality studies)
--
-- Apply via:
--   docker exec clickhouse clickhouse-client < 20260405_001_add_ttl_latency_ohlcv.sql
--
-- Verify via:
--   docker exec clickhouse clickhouse-client -q \
--     "SELECT database, name, engine_full FROM system.tables \
--      WHERE database = 'hft' AND name IN ('latency_spans', 'ohlcv_1m_state') \
--      ORDER BY name"
--
-- Note: hft.latency_spans uses ingest_ts (Int64, nanoseconds) — TTL uses the same
--       toDate(toDateTime(ingest_ts / 1000000000)) pattern as hft.orders.
--       hft.ohlcv_1m_state uses bucket (DateTime) — TTL uses bucket directly.
--       ClickHouse schedules TTL merges asynchronously; data is not deleted instantly.

-- Up

-- =============================================================================
-- hft.latency_spans (90-day retention — diagnostic artifact)
-- =============================================================================

ALTER TABLE hft.latency_spans
    MODIFY TTL toDate(toDateTime(ingest_ts / 1000000000)) + INTERVAL 90 DAY;

-- =============================================================================
-- hft.ohlcv_1m_state (365-day retention — 1 year of OHLCV history)
-- =============================================================================

ALTER TABLE hft.ohlcv_1m_state
    MODIFY TTL toDate(bucket) + INTERVAL 365 DAY;

-- Down (remove TTL — run to revert)
-- ALTER TABLE hft.latency_spans   MODIFY TTL '';
-- ALTER TABLE hft.ohlcv_1m_state  MODIFY TTL '';

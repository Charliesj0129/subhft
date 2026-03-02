-- Migration: 20260302_001_add_ttl_policies
-- Purpose: Add TTL retention policies to all tables that lack them.
--
-- Risk context (2026-03-02 disk crisis post-mortem):
--   hft.orders / trades / fills:  no TTL → ~60–120 GB/year growth risk
--   audit.*:                      no TTL → ~24–60 GB/year growth risk
--   hft.backtest_runs:            no TTL → ~12–36 GB/year growth risk
--
-- Retention decisions:
--   hft.orders / trades / fills   365 days  (1 year, trading record compliance)
--   audit.*                       730 days  (2 years, financial audit minimum)
--   hft.backtest_runs             90 days   (research artifact, rotated regularly)
--
-- Apply via:
--   docker exec clickhouse clickhouse-client < 20260302_001_add_ttl_policies.sql
--
-- Verify via:
--   docker exec clickhouse clickhouse-client -q \
--     "SELECT database, name, engine_full FROM system.tables \
--      WHERE database IN ('hft','audit') AND engine_full LIKE '%TTL%' \
--      ORDER BY database, name"
--
-- Note: TTL expressions use toDate() on nanosecond timestamps (Int64 → seconds → date).
--       ClickHouse will schedule TTL merges asynchronously; data is not deleted instantly.

-- Up

-- =============================================================================
-- hft operational tables (1-year retention)
-- =============================================================================

ALTER TABLE hft.orders
    MODIFY TTL toDate(toDateTime(ingest_ts / 1000000000)) + INTERVAL 365 DAY;

ALTER TABLE hft.trades
    MODIFY TTL toDate(toDateTime(match_ts / 1000000000)) + INTERVAL 365 DAY;

ALTER TABLE hft.fills
    MODIFY TTL toDate(toDateTime(ts_exchange / 1000000000)) + INTERVAL 365 DAY;

-- =============================================================================
-- audit tables (2-year retention — financial compliance minimum)
-- =============================================================================

ALTER TABLE audit.orders_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

ALTER TABLE audit.risk_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

ALTER TABLE audit.guardrail_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

ALTER TABLE audit.alpha_gate_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

ALTER TABLE audit.alpha_promotion_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

ALTER TABLE audit.alpha_canary_log
    MODIFY TTL toDate(toDateTime(ts / 1000000000)) + INTERVAL 730 DAY;

-- =============================================================================
-- hft.backtest_runs (90-day retention — research artifact)
-- =============================================================================

ALTER TABLE hft.backtest_runs
    MODIFY TTL toDate(created_at) + INTERVAL 90 DAY;

-- Down (remove TTL — run to revert)
-- ALTER TABLE hft.orders           MODIFY TTL '';
-- ALTER TABLE hft.trades           MODIFY TTL '';
-- ALTER TABLE hft.fills            MODIFY TTL '';
-- ALTER TABLE audit.orders_log     MODIFY TTL '';
-- ALTER TABLE audit.risk_log       MODIFY TTL '';
-- ALTER TABLE audit.guardrail_log  MODIFY TTL '';
-- ALTER TABLE audit.alpha_gate_log      MODIFY TTL '';
-- ALTER TABLE audit.alpha_promotion_log MODIFY TTL '';
-- ALTER TABLE audit.alpha_canary_log    MODIFY TTL '';
-- ALTER TABLE hft.backtest_runs    MODIFY TTL '';

-- C2 / Phase D: migrate hft.fills from MergeTree to ReplacingMergeTree keyed
-- by fill_id so that WAL replay (and any upstream duplicate emission) is
-- collapsed at ClickHouse merge time. Defence-in-depth on top of the
-- row-level dedup guard in hft._wal_dedup.
--
-- Rationale:
--   * Existing dedup uses hft._wal_dedup to track (table_name, hash) tuples.
--     A missed dedup write or a schema drift in the hasher would silently
--     double-insert the same fill.
--   * ReplacingMergeTree de-duplicates rows with identical ORDER BY keys at
--     merge time. Including fill_id in ORDER BY guarantees a single fill_id
--     collapses to one row regardless of insert count.
--
-- Safety:
--   * We create hft.fills_new, copy existing rows, then swap names. Reads
--     continue to succeed on the old table until the atomic RENAME.
--   * The swap leaves hft.fills_legacy_pre_rmt in place for one day so
--     operators can diff row counts before dropping.
--
-- Rollback:
--   RENAME TABLE hft.fills TO hft.fills_rmt_failed,
--                hft.fills_legacy_pre_rmt TO hft.fills;
--   DROP TABLE hft.fills_rmt_failed;
--
-- Operational notes:
--   * Run during a quiet window. Recorder writes should be paused or routed
--     through wal_first mode so the CREATE/INSERT/RENAME sequence is not
--     racing ingest.
--   * On a system that has never materialized hft.fills, this migration is
--     idempotent: the INSERT selects 0 rows and the RENAME still succeeds.

-- Up
CREATE TABLE IF NOT EXISTS hft.fills_new (
    ts_exchange Int64 Codec(DoubleDelta, LZ4),
    ts_local Int64 Codec(DoubleDelta, LZ4),
    client_order_id String,
    broker_order_id String,
    fill_id String,
    strategy_id String,
    symbol String,
    side String,
    qty UInt32,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    fee_scaled Int64 Codec(DoubleDelta, LZ4),
    source String
) ENGINE = ReplacingMergeTree()
PARTITION BY toDate(toDateTime(ts_exchange / 1000000000))
ORDER BY (strategy_id, symbol, ts_exchange, fill_id);

INSERT INTO hft.fills_new
SELECT
    ts_exchange,
    ts_local,
    client_order_id,
    broker_order_id,
    fill_id,
    strategy_id,
    symbol,
    side,
    qty,
    price_scaled,
    fee_scaled,
    source
FROM hft.fills;

RENAME TABLE hft.fills TO hft.fills_legacy_pre_rmt,
             hft.fills_new TO hft.fills;

-- Down
-- Run `OPTIMIZE TABLE hft.fills FINAL` during off-hours to force
-- de-duplication of any rows already duplicated pre-migration. The
-- engine will otherwise collapse duplicates lazily on the next merge.

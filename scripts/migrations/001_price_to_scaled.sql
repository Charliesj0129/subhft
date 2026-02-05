-- Migration: Float64 price → Int64 price_scaled
-- Scale factor: 1_000_000 (6 decimal places)
-- CAUTION: Run during maintenance window. Backup data first.

-- =============================================================================
-- Step 1: Create new tables with correct schema
-- =============================================================================

CREATE TABLE IF NOT EXISTS hft.market_data_v2 (
    symbol String,
    exchange String,
    type String,
    exch_ts Int64 Codec(DoubleDelta, LZ4),
    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    price_scaled Int64 Codec(Gorilla, LZ4),
    volume Int64 Codec(Gorilla, LZ4),
    bids_price Array(Int64) Codec(Gorilla, LZ4),
    bids_vol Array(Int64) Codec(Gorilla, LZ4),
    asks_price Array(Int64) Codec(Gorilla, LZ4),
    asks_vol Array(Int64) Codec(Gorilla, LZ4),
    seq_no UInt64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (symbol, exch_ts, ingest_ts)
TTL toDateTime(ingest_ts / 1000000000) + INTERVAL 6 MONTH;

CREATE TABLE IF NOT EXISTS hft.orders_v2 (
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    status String,
    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    latency_us Int64 Codec(Gorilla, LZ4)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (strategy_id, symbol, ingest_ts);

CREATE TABLE IF NOT EXISTS hft.trades_v2 (
    fill_id String,
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    fee_scaled Int64 Codec(Gorilla, LZ4),
    match_ts Int64 Codec(DoubleDelta, LZ4)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(match_ts / 1000000000))
ORDER BY (symbol, match_ts);

-- =============================================================================
-- Step 2: Migrate data (price * 1_000_000 → price_scaled)
-- =============================================================================

INSERT INTO hft.market_data_v2
SELECT
    symbol,
    exchange,
    type,
    exch_ts,
    ingest_ts,
    toInt64(price * 1000000) AS price_scaled,
    toInt64(volume) AS volume,
    arrayMap(x -> toInt64(x * 1000000), bids_price) AS bids_price,
    bids_vol,
    arrayMap(x -> toInt64(x * 1000000), asks_price) AS asks_price,
    asks_vol,
    seq_no
FROM hft.market_data;

INSERT INTO hft.orders_v2
SELECT
    order_id,
    strategy_id,
    symbol,
    side,
    toInt64(price * 1000000) AS price_scaled,
    qty,
    status,
    ingest_ts,
    toInt64(latency_us) AS latency_us
FROM hft.orders;

INSERT INTO hft.trades_v2
SELECT
    fill_id,
    order_id,
    strategy_id,
    symbol,
    side,
    toInt64(price * 1000000) AS price_scaled,
    qty,
    toInt64(fee * 1000000) AS fee_scaled,
    match_ts
FROM hft.trades;

-- =============================================================================
-- Step 3: Atomic table swap (rename)
-- =============================================================================

RENAME TABLE
    hft.market_data TO hft.market_data_backup,
    hft.market_data_v2 TO hft.market_data;

RENAME TABLE
    hft.orders TO hft.orders_backup,
    hft.orders_v2 TO hft.orders;

RENAME TABLE
    hft.trades TO hft.trades_backup,
    hft.trades_v2 TO hft.trades;

-- =============================================================================
-- Step 4: Cleanup (run AFTER verification - uncomment when ready)
-- =============================================================================

-- Verify row counts match before dropping:
-- SELECT 'market_data', count() FROM hft.market_data UNION ALL
-- SELECT 'market_data_backup', count() FROM hft.market_data_backup UNION ALL
-- SELECT 'orders', count() FROM hft.orders UNION ALL
-- SELECT 'orders_backup', count() FROM hft.orders_backup UNION ALL
-- SELECT 'trades', count() FROM hft.trades UNION ALL
-- SELECT 'trades_backup', count() FROM hft.trades_backup;

-- DROP TABLE IF EXISTS hft.market_data_backup;
-- DROP TABLE IF EXISTS hft.orders_backup;
-- DROP TABLE IF EXISTS hft.trades_backup;

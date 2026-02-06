-- Database
CREATE DATABASE IF NOT EXISTS hft;

-- Scale factor for price_scaled: 1_000_000 (micros, 6 decimal places)
-- To convert: price_scaled = price_float * 1_000_000
-- To read:    price_float = price_scaled / 1_000_000.0

-- Market Data (Tick + BidAsk + Snapshot normalized)
-- Partition by YYYYMMDD for efficient management
CREATE TABLE IF NOT EXISTS hft.market_data (
    symbol String,
    exchange String,
    type String, -- 'Tick', 'BidAsk', 'Snapshot'

    -- Timestamps (ns)
    exch_ts Int64 Codec(DoubleDelta, LZ4),
    ingest_ts Int64 Codec(DoubleDelta, LZ4),

    -- Pricing (Fixed-point scaled, NOT Float - Precision Law)
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    volume Int64 Codec(DoubleDelta, LZ4),

    -- LOB (Arrays for variable depth, usually 1 or 5)
    -- Prices as scaled Int64, volumes as Int64
    bids_price Array(Int64) Codec(LZ4),
    bids_vol Array(Int64) Codec(LZ4),
    asks_price Array(Int64) Codec(LZ4),
    asks_vol Array(Int64) Codec(LZ4),

    -- Flags
    seq_no UInt64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (symbol, exch_ts, ingest_ts)
TTL toDateTime(ingest_ts / 1000000000) + INTERVAL 6 MONTH;

-- Orders
CREATE TABLE IF NOT EXISTS hft.orders (
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    status String, -- 'FILLED', 'NEW'

    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    latency_us Int64 Codec(DoubleDelta, LZ4) -- Internal system latency (microseconds)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (strategy_id, symbol, ingest_ts);

-- Fills
CREATE TABLE IF NOT EXISTS hft.trades (
    fill_id String,
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    fee_scaled Int64 Codec(DoubleDelta, LZ4),

    match_ts Int64 Codec(DoubleDelta, LZ4)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(match_ts / 1000000000))
ORDER BY (symbol, match_ts);

-- =============================================================================
-- Materialized Views for Real-Time Analytics
-- =============================================================================

-- OHLCV 1-minute candles (pre-aggregated from ticks)
CREATE TABLE IF NOT EXISTS hft.ohlcv_1m (
    symbol String,
    exchange String,
    bucket DateTime Codec(DoubleDelta, LZ4),

    open_scaled Int64 Codec(DoubleDelta, LZ4),
    high_scaled Int64 Codec(DoubleDelta, LZ4),
    low_scaled Int64 Codec(DoubleDelta, LZ4),
    close_scaled Int64 Codec(DoubleDelta, LZ4),
    volume Int64 Codec(DoubleDelta, LZ4),
    tick_count UInt64
) ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(bucket)
ORDER BY (symbol, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS hft.ohlcv_1m_mv
TO hft.ohlcv_1m AS
SELECT
    symbol,
    exchange,
    toStartOfMinute(toDateTime(exch_ts / 1000000000)) AS bucket,
    argMin(price_scaled, exch_ts) AS open_scaled,
    max(price_scaled) AS high_scaled,
    min(price_scaled) AS low_scaled,
    argMax(price_scaled, exch_ts) AS close_scaled,
    sum(volume) AS volume,
    count() AS tick_count
FROM hft.market_data
WHERE type = 'Tick' AND price_scaled > 0
GROUP BY symbol, exchange, bucket;

-- Latency Statistics (P50, P95, P99 per minute)
CREATE TABLE IF NOT EXISTS hft.latency_stats_1m (
    bucket DateTime Codec(DoubleDelta, LZ4),
    strategy_id String,

    order_count UInt64,
    latency_p50 Int64 Codec(DoubleDelta, LZ4),
    latency_p95 Int64 Codec(DoubleDelta, LZ4),
    latency_p99 Int64 Codec(DoubleDelta, LZ4),
    latency_max Int64 Codec(DoubleDelta, LZ4)
) ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(bucket)
ORDER BY (strategy_id, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS hft.latency_stats_1m_mv
TO hft.latency_stats_1m AS
SELECT
    toStartOfMinute(toDateTime(ingest_ts / 1000000000)) AS bucket,
    strategy_id,
    count() AS order_count,
    quantile(0.50)(latency_us) AS latency_p50,
    quantile(0.95)(latency_us) AS latency_p95,
    quantile(0.99)(latency_us) AS latency_p99,
    max(latency_us) AS latency_max
FROM hft.orders
GROUP BY strategy_id, bucket;

-- Pipeline latency spans (sampled)
CREATE TABLE IF NOT EXISTS hft.latency_spans (
    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    stage LowCardinality(String),
    latency_us Int64 Codec(DoubleDelta, LZ4),
    trace_id String,
    symbol LowCardinality(String),
    strategy_id LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (stage, ingest_ts);

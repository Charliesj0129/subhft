-- Database
CREATE DATABASE IF NOT EXISTS hft;

-- Market Data (Tick + BidAsk + Snapshot normalized)
-- Partition by YYYYMMDD for efficient management
CREATE TABLE IF NOT EXISTS hft.market_data (
    symbol String,
    exchange String,
    type String, -- 'Tick', 'BidAsk', 'Snapshot'
    
    -- Timestamps (ns)
    exch_ts Int64 Codec(DoubleDelta, LZ4),
    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    
    -- Pricing
    price Float64 Codec(Gorilla, LZ4),
    volume Float64,
    
    -- LOB (Arrays for variable depth, usually 1 or 5)
    bids_price Array(Float64),
    bids_vol Array(Int64),
    asks_price Array(Float64),
    asks_vol Array(Int64),
    
    -- Flags
    seq_no UInt64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (symbol, exch_ts, ingest_ts)
TTL toDateTime(ingest_ts / 1000000000) + INTERVAL 1 YEAR;

-- Orders
CREATE TABLE IF NOT EXISTS hft.orders (
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price Float64,
    qty Int64,
    status String, -- 'FILLED', 'NEW'
    
    ingest_ts Int64,
    latency_us Float64 -- Internal system latency
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
    price Float64,
    qty Int64,
    fee Float64,
    
    match_ts Int64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(match_ts / 1000000000))
ORDER BY (symbol, match_ts);

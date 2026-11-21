-- src/hft_platform/migrations/clickhouse/20261121_001_create_hist_market_data.sql
-- Historical market data pulled after the fact from broker query APIs
-- (Shioaji api.ticks / api.kbars, Fubon futopt historical/candles). Kept apart
-- from hft.market_data: that table is partitioned and TTL'd by ingest_ts and
-- feeds ohlcv_1m_mv, so a backfill there lands in today's partition and
-- pollutes the live 1m bars.
--
-- Prices are Int64 x1,000,000 (research ClickHouse scale). Timestamps are UTC
-- epoch ns. contract_code is the real delivery-month code; query_code is what
-- was asked for (TXFR1, 1!, ...), so a roll day can be told from a price jump.
-- ReplacingMergeTree on ingest_ts: re-running a day replaces, never duplicates.
-- Read with FINAL. No TTL: this is the long-term archive.
-- Up
CREATE TABLE IF NOT EXISTS hft.hist_ticks (
    source LowCardinality(String),
    product LowCardinality(String),
    query_code LowCardinality(String),
    contract_code LowCardinality(String),
    trading_day Date,
    session Enum8('day' = 1, 'night' = 2),
    exch_ts Int64 Codec(DoubleDelta, LZ4),
    seq UInt32,
    price_scaled Int64 Codec(Delta, LZ4),
    volume Int64,
    bid_scaled Int64 Codec(Delta, LZ4),
    ask_scaled Int64 Codec(Delta, LZ4),
    tick_type Int8,
    ingest_ts Int64
) ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(trading_day)
ORDER BY (source, product, trading_day, exch_ts, seq);

CREATE TABLE IF NOT EXISTS hft.hist_kbars (
    source LowCardinality(String),
    product LowCardinality(String),
    query_code LowCardinality(String),
    contract_code LowCardinality(String),
    timeframe_min UInt16,
    session Enum8('day' = 1, 'night' = 2),
    bar_ts Int64 Codec(DoubleDelta, LZ4),
    open_scaled Int64,
    high_scaled Int64,
    low_scaled Int64,
    close_scaled Int64,
    volume Int64,
    ingest_ts Int64
) ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(toDate(intDiv(bar_ts, 1000000000)))
ORDER BY (source, product, timeframe_min, bar_ts);

CREATE TABLE IF NOT EXISTS hft.hist_ingest_watermark (
    source LowCardinality(String),
    product LowCardinality(String),
    kind Enum8('ticks' = 1, 'kbars' = 2),
    trading_day Date,
    status Enum8('DONE' = 1, 'EMPTY' = 2, 'PENDING_QUOTA' = 3, 'FAILED' = 4),
    rows UInt64,
    contract_code LowCardinality(String),
    usage_bytes_after Int64,
    note String,
    updated_ts Int64
) ENGINE = ReplacingMergeTree(updated_ts)
ORDER BY (source, product, kind, trading_day);

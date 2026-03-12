-- Up
DROP VIEW IF EXISTS hft.kline_1m_view;
DROP VIEW IF EXISTS hft.candles_1m_mv;
DROP TABLE IF EXISTS hft.ohlcv_1m_mv;
DROP TABLE IF EXISTS hft.ohlcv_1m;
DROP TABLE IF EXISTS hft.ohlcv_1m_state;

CREATE TABLE IF NOT EXISTS hft.ohlcv_1m_state (
    symbol String,
    exchange String,
    bucket DateTime Codec(DoubleDelta, LZ4),
    open_state AggregateFunction(argMin, Int64, Int64),
    high_state AggregateFunction(max, Int64),
    low_state AggregateFunction(min, Int64),
    close_state AggregateFunction(argMax, Int64, Int64),
    volume_state AggregateFunction(sum, Int64),
    tick_count_state AggregateFunction(count)
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(bucket)
ORDER BY (symbol, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS hft.ohlcv_1m_mv
TO hft.ohlcv_1m_state AS
SELECT
    symbol,
    exchange,
    toStartOfMinute(toDateTime(exch_ts / 1000000000)) AS bucket,
    argMinState(price_scaled, exch_ts) AS open_state,
    maxState(price_scaled) AS high_state,
    minState(price_scaled) AS low_state,
    argMaxState(price_scaled, exch_ts) AS close_state,
    sumState(volume) AS volume_state,
    countState() AS tick_count_state
FROM hft.market_data
WHERE type = 'Tick' AND price_scaled > 0
GROUP BY symbol, exchange, bucket;

CREATE VIEW IF NOT EXISTS hft.ohlcv_1m AS
SELECT
    symbol,
    exchange,
    bucket,
    argMinMerge(open_state) AS open_scaled,
    maxMerge(high_state) AS high_scaled,
    minMerge(low_state) AS low_scaled,
    argMaxMerge(close_state) AS close_scaled,
    sumMerge(volume_state) AS volume,
    countMerge(tick_count_state) AS tick_count
FROM hft.ohlcv_1m_state
GROUP BY symbol, exchange, bucket;

CREATE VIEW IF NOT EXISTS hft.candles_1m_mv AS
SELECT
    symbol,
    exchange,
    bucket AS window,
    open_scaled,
    high_scaled,
    low_scaled,
    close_scaled,
    volume,
    tick_count
FROM hft.ohlcv_1m;

CREATE VIEW IF NOT EXISTS hft.kline_1m_view AS
SELECT
    symbol,
    bucket AS window,
    open_scaled / 1000000.0 AS open,
    high_scaled / 1000000.0 AS high,
    low_scaled / 1000000.0 AS low,
    close_scaled / 1000000.0 AS close,
    volume * 1.0 AS volume,
    tick_count AS ticks
FROM hft.ohlcv_1m;

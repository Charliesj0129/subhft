-- Up
CREATE TABLE IF NOT EXISTS hft.pnl_snapshots (
    snapshot_ts  Int64 Codec(DoubleDelta, LZ4),  -- ns epoch
    account_id   String,
    strategy_id  LowCardinality(String),
    symbol       LowCardinality(String),

    net_qty            Int64,
    avg_price_scaled   Int64 Codec(DoubleDelta, LZ4),
    realized_pnl_scaled Int64 Codec(DoubleDelta, LZ4),
    fees_scaled        Int64 Codec(DoubleDelta, LZ4),
    total_pnl_scaled   Int64 Codec(DoubleDelta, LZ4),
    peak_equity_scaled Int64 Codec(DoubleDelta, LZ4),
    drawdown_pct       Float32
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(snapshot_ts / 1000000000))
ORDER BY (strategy_id, symbol, snapshot_ts)
TTL toDateTime(snapshot_ts / 1000000000) + INTERVAL 90 DAY;

-- Down
-- DROP TABLE IF EXISTS hft.pnl_snapshots;

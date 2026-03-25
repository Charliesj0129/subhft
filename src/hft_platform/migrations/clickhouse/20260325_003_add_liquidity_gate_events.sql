CREATE TABLE IF NOT EXISTS hft.liquidity_gate_events (
    symbol        String,
    spread_scaled Int64,
    threshold_scaled Int64,
    action        String,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 30 DAY;

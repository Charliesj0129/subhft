CREATE TABLE IF NOT EXISTS hft.liquidity_gate_events (
    symbol    String,
    result    String,
    spread_pts Float64,
    threshold  Float64,
    ts         Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 90 DAY;

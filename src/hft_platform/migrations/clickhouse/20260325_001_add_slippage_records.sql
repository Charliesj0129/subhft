CREATE TABLE IF NOT EXISTS hft.slippage_records (
    order_id      String,
    symbol        String,
    side          UInt8,
    decision_mid  Int64,
    fill_price    Int64,
    slippage_ticks Int32,
    slippage_ntd  Int32,
    latency_ns    Int64,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 90 DAY;

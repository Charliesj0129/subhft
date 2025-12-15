CREATE TABLE IF NOT EXISTS fills (
    ts_exchange DateTime64(6),
    ts_local DateTime64(6),
    client_order_id String,
    broker_order_id String,
    fill_id String,
    strategy_id String,
    symbol String,
    side String,
    qty UInt32,
    price Float64,
    fee Float64,
    source String
) ENGINE = MergeTree()
PARTITION BY toDate(ts_exchange)
ORDER BY (strategy_id, symbol, ts_exchange);

-- WU-10: Shadow orders table
CREATE TABLE IF NOT EXISTS hft.shadow_orders
(
    ts_ns       UInt64,
    strategy_id LowCardinality(String),
    symbol      LowCardinality(String),
    side        LowCardinality(String),
    price       Int64,
    qty         Int32,
    intent_type LowCardinality(String),
    intent_id   String,
    inserted_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree()
ORDER BY (strategy_id, symbol, ts_ns, intent_id)
TTL inserted_at + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;

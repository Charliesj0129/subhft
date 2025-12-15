-- Market Data Table
CREATE TABLE IF NOT EXISTS market_data (
    timestamp DateTime64(6),
    symbol LowCardinality(String),
    message_type String, -- 'quote', 'trade'
    price Float64,
    quantity Float64,
    bid_price_1 Float64,
    ask_price_1 Float64,
    bid_qty_1 Float64,
    ask_qty_1 Float64
) ENGINE = MergeTree()
ORDER BY (symbol, timestamp);

-- Orders Table
CREATE TABLE IF NOT EXISTS orders (
    order_id String,
    timestamp DateTime64(6),
    symbol LowCardinality(String),
    side String, -- 'buy', 'sell'
    price Float64,
    quantity Float64,
    status String,
    latency_us UInt64
) ENGINE = MergeTree()
ORDER BY (symbol, timestamp);

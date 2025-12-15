CREATE TABLE IF NOT EXISTS market_data (
    ts_exchange DateTime64(6),
    ts_local DateTime64(6),
    symbol String,
    
    bid_px_0 Float64,
    bid_sz_0 UInt32,
    ask_px_0 Float64,
    ask_sz_0 UInt32,
    
    bid_px_1 Float64,
    bid_sz_1 UInt32,
    ask_px_1 Float64,
    ask_sz_1 UInt32,
    -- (Can expand to top-5 if needed)
    
    source String
) ENGINE = MergeTree()
PARTITION BY toDate(ts_exchange)
ORDER BY (symbol, ts_exchange);

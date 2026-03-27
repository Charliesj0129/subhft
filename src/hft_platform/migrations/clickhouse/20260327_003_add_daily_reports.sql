CREATE TABLE IF NOT EXISTS hft.daily_reports (
    date          Date,
    strategy_id   String,
    symbol        String,
    realized_pnl  Int64,
    unrealized_pnl Int64,
    trade_count   UInt32,
    fill_count    UInt32,
    total_cost_ntd Int64,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (date, strategy_id, symbol)
TTL date + INTERVAL 1 YEAR;

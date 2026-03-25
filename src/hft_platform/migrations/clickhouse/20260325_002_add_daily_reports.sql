CREATE TABLE IF NOT EXISTS hft.daily_reports (
    report_date   Date,
    strategy_id   String,
    symbol        String,
    realized_pnl_ntd  Int32,
    unrealized_pnl_ntd Int32,
    net_pnl_ntd   Int32,
    fees_ntd      Int32,
    tax_ntd       Int32,
    orders_sent   UInt32,
    orders_filled UInt32,
    orders_cancelled UInt32,
    avg_slippage_ticks Float32,
    slippage_cost_ntd Int32,
    peak_pnl_ntd  Int32,
    max_drawdown_ntd Int32,
    soft_limit_triggers UInt32,
    hard_limit_triggers UInt32,
    autonomy_transitions UInt32,
    win_count     UInt32,
    loss_count    UInt32,
    profit_factor Float32,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (report_date, strategy_id)
TTL report_date + INTERVAL 365 DAY;

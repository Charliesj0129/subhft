CREATE TABLE IF NOT EXISTS backtest_timeseries (
    run_id UUID,
    ts DateTime64(9),
    
    equity Float64,
    gross_exposure Float64,
    net_exposure Float64,
    drawdown_pct Float64,
    
    -- Optional breakdown
    pnl_realized Float64,
    pnl_unrealized Float64
    
) ENGINE = MergeTree()
ORDER BY (run_id, ts)
TTL ts + INTERVAL 30 DAY;

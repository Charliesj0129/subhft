CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id UUID,
    strategy_id String,
    git_commit String,
    config_hash String,
    start_ts DateTime64(9),
    end_ts DateTime64(9),
    
    -- Config Snapshot (JSON)
    config_json String,
    
    -- Results
    total_pnl Float64,
    sharpe_ratio Float64,
    max_drawdown Float64,
    win_rate Float64,
    total_turnover Float64,
    total_trades UInt64,
    runtime_seconds Float64,
    
    created_at DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (strategy_id, start_ts);

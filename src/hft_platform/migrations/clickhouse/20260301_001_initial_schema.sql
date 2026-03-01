-- Up
-- Database
CREATE DATABASE IF NOT EXISTS hft;

-- Scale factor for price_scaled: 1_000_000 (micros, 6 decimal places)
-- To convert: price_scaled = price_float * 1_000_000
-- To read:    price_float = price_scaled / 1_000_000.0

-- Market Data (Tick + BidAsk + Snapshot normalized)
-- Partition by YYYYMMDD for efficient management
CREATE TABLE IF NOT EXISTS hft.market_data (
    symbol String,
    exchange String,
    type String, -- 'Tick', 'BidAsk', 'Snapshot'

    -- Timestamps (ns)
    exch_ts Int64 Codec(DoubleDelta, LZ4),
    ingest_ts Int64 Codec(DoubleDelta, LZ4),

    -- Pricing (Fixed-point scaled, NOT Float - Precision Law)
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    volume Int64 Codec(DoubleDelta, LZ4),

    -- LOB (Arrays for variable depth, usually 1 or 5)
    -- Prices as scaled Int64, volumes as Int64
    bids_price Array(Int64) Codec(LZ4),
    bids_vol Array(Int64) Codec(LZ4),
    asks_price Array(Int64) Codec(LZ4),
    asks_vol Array(Int64) Codec(LZ4),

    -- Flags
    seq_no UInt64
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (symbol, exch_ts, ingest_ts)
TTL toDateTime(ingest_ts / 1000000000) + INTERVAL 6 MONTH;

-- Orders
CREATE TABLE IF NOT EXISTS hft.orders (
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    status String, -- 'FILLED', 'NEW'

    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    latency_us Int64 Codec(DoubleDelta, LZ4) -- Internal system latency (microseconds)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (strategy_id, symbol, ingest_ts);

-- Fills
CREATE TABLE IF NOT EXISTS hft.trades (
    fill_id String,
    order_id String,
    strategy_id String,
    symbol String,
    side String,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    qty Int64,
    fee_scaled Int64 Codec(DoubleDelta, LZ4),

    match_ts Int64 Codec(DoubleDelta, LZ4)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(match_ts / 1000000000))
ORDER BY (symbol, match_ts);

-- =============================================================================
-- Materialized Views for Real-Time Analytics
-- =============================================================================

-- Legacy cleanup: drop old MV that referenced `price` (pre-scale schema)
DROP TABLE IF EXISTS hft.candles_1m_mv;

-- OHLCV 1-minute candles (pre-aggregated from ticks)
CREATE TABLE IF NOT EXISTS hft.ohlcv_1m (
    symbol String,
    exchange String,
    bucket DateTime Codec(DoubleDelta, LZ4),

    open_scaled Int64 Codec(DoubleDelta, LZ4),
    high_scaled Int64 Codec(DoubleDelta, LZ4),
    low_scaled Int64 Codec(DoubleDelta, LZ4),
    close_scaled Int64 Codec(DoubleDelta, LZ4),
    volume Int64 Codec(DoubleDelta, LZ4),
    tick_count UInt64
) ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(bucket)
ORDER BY (symbol, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS hft.ohlcv_1m_mv
TO hft.ohlcv_1m AS
SELECT
    symbol,
    exchange,
    toStartOfMinute(toDateTime(exch_ts / 1000000000)) AS bucket,
    argMin(price_scaled, exch_ts) AS open_scaled,
    max(price_scaled) AS high_scaled,
    min(price_scaled) AS low_scaled,
    argMax(price_scaled, exch_ts) AS close_scaled,
    sum(volume) AS volume,
    count() AS tick_count
FROM hft.market_data
WHERE type = 'Tick' AND price_scaled > 0
GROUP BY symbol, exchange, bucket;

-- Compatibility view for legacy dashboards/queries
CREATE VIEW IF NOT EXISTS hft.candles_1m_mv AS
SELECT
    symbol,
    exchange,
    bucket AS window,
    open_scaled,
    high_scaled,
    low_scaled,
    close_scaled,
    volume,
    tick_count
FROM hft.ohlcv_1m;

-- Latency Statistics (P50, P95, P99 per minute)
CREATE TABLE IF NOT EXISTS hft.latency_stats_1m (
    bucket DateTime Codec(DoubleDelta, LZ4),
    strategy_id String,

    order_count UInt64,
    latency_p50 Int64 Codec(DoubleDelta, LZ4),
    latency_p95 Int64 Codec(DoubleDelta, LZ4),
    latency_p99 Int64 Codec(DoubleDelta, LZ4),
    latency_max Int64 Codec(DoubleDelta, LZ4)
) ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(bucket)
ORDER BY (strategy_id, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS hft.latency_stats_1m_mv
TO hft.latency_stats_1m AS
SELECT
    toStartOfMinute(toDateTime(ingest_ts / 1000000000)) AS bucket,
    strategy_id,
    count() AS order_count,
    quantile(0.50)(latency_us) AS latency_p50,
    quantile(0.95)(latency_us) AS latency_p95,
    quantile(0.99)(latency_us) AS latency_p99,
    max(latency_us) AS latency_max
FROM hft.orders
GROUP BY strategy_id, bucket;

-- Pipeline latency spans (sampled)
CREATE TABLE IF NOT EXISTS hft.latency_spans (
    ingest_ts Int64 Codec(DoubleDelta, LZ4),
    stage LowCardinality(String),
    latency_us Int64 Codec(DoubleDelta, LZ4),
    trace_id String,
    symbol LowCardinality(String),
    strategy_id LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(ingest_ts / 1000000000))
ORDER BY (stage, ingest_ts);

-- Database for Audit Logs
CREATE DATABASE IF NOT EXISTS audit;

-- Orders Log: Append-only ledger of all intents and outcomes
CREATE TABLE IF NOT EXISTS audit.orders_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    strategy_id String,
    intent_id String,
    order_id String,
    symbol String,
    action String, -- NEW, AMEND, CANCEL
    price Int64,
    qty Int32,
    side String,
    status String,
    broker_msg String,
    latency_us UInt32
) ENGINE = MergeTree()
ORDER BY (strategy_id, ts, symbol);

-- Risk Log: Decisions made by RiskEngine
CREATE TABLE IF NOT EXISTS audit.risk_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    strategy_id String,
    intent_id String,
    check_name String,
    approved UInt8, -- 1=True, 0=False
    reason String,
    threshold Float64,
    value Float64
) ENGINE = MergeTree()
ORDER BY (strategy_id, ts);

-- Guardrail Log: StormGuard Transitions
CREATE TABLE IF NOT EXISTS audit.guardrail_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    strategy_id String,
    old_state String,
    new_state String,
    pnl_drawdown Float64
) ENGINE = MergeTree()
ORDER BY (ts, strategy_id);

-- Alpha Gate Log: one row per gate evaluation (A-E)
CREATE TABLE IF NOT EXISTS audit.alpha_gate_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    alpha_id String,
    run_id String,
    gate LowCardinality(String),       -- 'A','B','C','D','E'
    passed UInt8,
    config_hash String,
    details String                      -- JSON blob
) ENGINE = MergeTree()
ORDER BY (alpha_id, ts, gate);

-- Alpha Promotion Log: one row per promote_alpha call
CREATE TABLE IF NOT EXISTS audit.alpha_promotion_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    alpha_id String,
    run_id String,
    approved UInt8,
    forced UInt8,
    gate_d_passed UInt8,
    gate_e_passed UInt8,
    canary_weight Float64,
    config_hash String,
    reasons String,                     -- JSON array
    scorecard String                    -- JSON blob
) ENGINE = MergeTree()
ORDER BY (alpha_id, ts);

-- Alpha Canary Log: one row per canary evaluation action
CREATE TABLE IF NOT EXISTS audit.alpha_canary_log (
    ts Int64 Codec(DoubleDelta, LZ4),
    alpha_id String,
    action LowCardinality(String),      -- 'hold','escalate','rollback','graduate'
    old_weight Float64,
    new_weight Float64,
    reason String,
    checks String                       -- JSON blob
) ENGINE = MergeTree()
ORDER BY (alpha_id, ts);

CREATE TABLE IF NOT EXISTS hft.fills (
    ts_exchange Int64 Codec(DoubleDelta, LZ4),
    ts_local Int64 Codec(DoubleDelta, LZ4),
    client_order_id String,
    broker_order_id String,
    fill_id String,
    strategy_id String,
    symbol String,
    side String,
    qty UInt32,
    price_scaled Int64 Codec(DoubleDelta, LZ4),
    fee_scaled Int64 Codec(DoubleDelta, LZ4),
    source String
) ENGINE = MergeTree()
PARTITION BY toDate(toDateTime(ts_exchange / 1000000000))
ORDER BY (strategy_id, symbol, ts_exchange);

CREATE TABLE IF NOT EXISTS hft.backtest_runs (
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

CREATE TABLE IF NOT EXISTS hft.backtest_timeseries (
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

-- Down
-- DROP TABLE IF EXISTS hft.backtest_timeseries;
-- DROP TABLE IF EXISTS hft.backtest_runs;
-- DROP TABLE IF EXISTS hft.fills;
-- DROP DATABASE IF EXISTS audit;
-- DROP DATABASE IF EXISTS hft;

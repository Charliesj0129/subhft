-- Database for Audit Logs
CREATE DATABASE IF NOT EXISTS audit;

-- Orders Log: Append-only ledger of all intents and outcomes
CREATE TABLE IF NOT EXISTS audit.orders_log (
    ts DateTime64(6),
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
    ts DateTime64(6),
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
    ts DateTime64(6),
    strategy_id String,
    old_state String,
    new_state String,
    pnl_drawdown Float64
) ENGINE = MergeTree()
ORDER BY (ts, strategy_id);

-- Alpha Gate Log: one row per gate evaluation (A-E)
CREATE TABLE IF NOT EXISTS audit.alpha_gate_log (
    ts DateTime64(6),
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
    ts DateTime64(6),
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
    ts DateTime64(6),
    alpha_id String,
    action LowCardinality(String),      -- 'hold','escalate','rollback','graduate'
    old_weight Float64,
    new_weight Float64,
    reason String,
    checks String                       -- JSON blob
) ENGINE = MergeTree()
ORDER BY (alpha_id, ts);

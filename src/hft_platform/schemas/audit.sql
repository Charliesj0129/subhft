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

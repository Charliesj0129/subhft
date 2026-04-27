-- P1-a (Infra Fixer 2026-04-27): align audit.* table DDLs with the actual
-- producer payloads emitted by RiskEngine, StormGuard, and OrderAdapter.
--
-- Root cause:
--   The original 20260301_001_initial_schema.sql DDL for audit.orders_log /
--   audit.risk_log / audit.guardrail_log was designed against an earlier
--   producer scheme that no longer exists. The current producers emit:
--
--   audit.risk_log  (RiskEngine._audit_risk_decision):
--     ts_ns, strategy_id, symbol, intent_type (int), price (int x10000),
--     qty (int), approved (bool), reason_code (str)
--
--   audit.guardrail_log (StormGuard._transition):
--     ts_ns, old_state, new_state, reason
--
--   audit.orders_log (OrderAdapter._audit_log_order, multiple call sites):
--     ts_ns, event (str), intent_type (str), order_key, target_key (opt),
--     symbol, side (opt), price (opt), qty (opt), strategy_id, cmd_id,
--     new_price (amend), error (failure paths)
--
--   The legacy DDL columns (ts/intent_id/check_name/threshold/value etc.)
--   were never populated. Every batched INSERT raised KeyError-on-column
--   in clickhouse-driver and silently fell back to structlog audit_fallback
--   (see Bug #19 / services/system.py:443-462).
--
-- Strategy choice:
--   STRATEGY A (modify DDL to match producers). Producer payloads are
--   richer (multi-event order lifecycle, symbol context, cmd_id) and
--   already shipping; DDL is the side that diverged. Tables were empty
--   per audit-investigator findings (see debug-team report 2026-04-27),
--   so DROP+CREATE is safe with no data loss.
--
-- Operational notes:
--   * audit.orders_log includes a `details` String column for forward-
--     compatibility — multiple producer call sites emit different optional
--     fields. The non-conformant ones land in details (JSON-encoded).
--   * ts_ns is named consistently across all three tables so the auditor
--     query layer doesn't need per-table conditionals.
--
-- Rollback:
--   The original DDL is preserved in 20260301_001_initial_schema.sql.
--   To rollback: DROP each table and re-run the relevant CREATE block from
--   that file. (We don't auto-restore because the new schema captures
--   strictly more information.)

-- Up

DROP TABLE IF EXISTS audit.risk_log;
CREATE TABLE audit.risk_log (
    ts_ns Int64 Codec(DoubleDelta, LZ4),
    strategy_id LowCardinality(String),
    symbol LowCardinality(String),
    intent_type Int32,
    price Int64,
    qty Int32,
    approved UInt8,
    reason_code String
) ENGINE = MergeTree()
ORDER BY (strategy_id, ts_ns, symbol)
TTL toDateTime(ts_ns / 1000000000) + INTERVAL 90 DAY DELETE;

DROP TABLE IF EXISTS audit.guardrail_log;
CREATE TABLE audit.guardrail_log (
    ts_ns Int64 Codec(DoubleDelta, LZ4),
    old_state String,
    new_state String,
    reason String
) ENGINE = MergeTree()
ORDER BY (ts_ns)
TTL toDateTime(ts_ns / 1000000000) + INTERVAL 365 DAY DELETE;

DROP TABLE IF EXISTS audit.orders_log;
CREATE TABLE audit.orders_log (
    ts_ns Int64 Codec(DoubleDelta, LZ4),
    event LowCardinality(String),         -- 'dispatched','dispatch_failed','cancel_no_op_*'
    intent_type LowCardinality(String),    -- 'NEW','AMEND','CANCEL','FORCE_FLAT'
    order_key String,
    target_key String,                     -- '' when not applicable
    symbol LowCardinality(String),
    side LowCardinality(String),           -- 'BUY','SELL', or '' when N/A
    price Int64,                           -- 0 when not applicable
    new_price Int64,                       -- AMEND only, 0 otherwise
    qty Int32,                             -- 0 when not applicable
    strategy_id LowCardinality(String),
    cmd_id Int64,
    error String,                          -- '' on success
    details String                         -- JSON blob for forward-compat
) ENGINE = MergeTree()
ORDER BY (strategy_id, ts_ns, symbol)
TTL toDateTime(ts_ns / 1000000000) + INTERVAL 180 DAY DELETE;

-- ============================================================
-- Down (manual rollback — run if you need to revert this migration).
-- ============================================================
-- DROP TABLE IF EXISTS audit.risk_log;
-- DROP TABLE IF EXISTS audit.guardrail_log;
-- DROP TABLE IF EXISTS audit.orders_log;
-- Then re-run the matching CREATE blocks from
-- 20260301_001_initial_schema.sql (look for `audit.risk_log`,
-- `audit.guardrail_log`, `audit.orders_log` sections).

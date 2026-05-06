-- 20260505_003_create_order_explanations.sql
-- L8 (loop_v1 convergence): canonical per-order explanation table.
--
-- Why a denormalized append-only table:
--   Today an order's reason is scattered across hft.orders, hft.fills,
--   hft.order_intents, decision-trace JSONL, strategy logs, and risk logs.
--   Joining at query time over 7+ surfaces is fragile (TTLs differ, fills
--   fan in, traces sample). This table is the canonical "why was this
--   order placed and what happened" row, emitted exactly once per order
--   when the lifecycle reaches a terminal state.
--
-- Population:
--   Written by recorder/worker.py (topic "explanations") behind
--   HFT_EXPLANATION_RECORDER_ENABLED=1, auto-enabled when settings.loop_id
--   is set. The OrderExplanationAssembler in src/hft_platform/order/
--   explanation.py emits one row per (trace_id, client_order_id) pair on
--   terminal/sweep.
--
-- Activation:
--   The table is created idempotently here. Until the assembler is wired
--   (L8 follow-on tasks) the table stays empty -- no producers exist yet.
--
-- Join keys:
--   (loop_id, ts_emit, trace_id) for time-bounded scans of a deployed
--   loop's orders. client_order_id is the secondary join into hft.fills
--   and hft.orders for cross-validation.

-- Up
CREATE TABLE IF NOT EXISTS hft.order_explanations (
    trace_id            String,
    client_order_id     String,
    loop_id             LowCardinality(String),
    strategy_id         LowCardinality(String),
    strategy_version    LowCardinality(String),
    config_hash         LowCardinality(String),
    git_sha             LowCardinality(String),
    data_session_id     LowCardinality(String),
    symbol              LowCardinality(String),
    -- JSON-encoded payloads. We avoid Map(String, *) so heterogeneous value
    -- types (numbers, strings, booleans, nested) survive without schema
    -- migrations every time a strategy adds a feature.
    feature_snapshot    String CODEC(ZSTD(3)),
    strategy_decision   String CODEC(ZSTD(3)),
    risk_decision       String CODEC(ZSTD(3)),
    `order`             String CODEC(ZSTD(3)),
    fills               String CODEC(ZSTD(3)),  -- JSON array of fill dicts
    cancels             String CODEC(ZSTD(3)),  -- JSON array of cancel dicts
    pnl_after           String CODEC(ZSTD(3)),  -- JSON object or empty string
    lifecycle_status    LowCardinality(String), -- filled / partial / canceled / rejected / incomplete
    ts_emit             Int64 CODEC(DoubleDelta, LZ4),
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime64(ts_emit/1e9, 3))
ORDER BY (loop_id, ts_emit, trace_id)
TTL toDate(toDateTime64(ts_emit/1e9, 3)) + INTERVAL 365 DAY  -- aligned with hft.orders 365d retention
SETTINGS index_granularity = 8192;

-- Down
-- DROP TABLE IF EXISTS hft.order_explanations;

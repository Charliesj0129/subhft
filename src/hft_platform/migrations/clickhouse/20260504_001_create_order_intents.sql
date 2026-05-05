-- src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql
-- Slice C: opt-in OrderIntent recorder topic.
-- Activated by HFT_INTENT_RECORDER_ENABLED=1; otherwise table stays empty.
CREATE TABLE IF NOT EXISTS hft.order_intents (
    intent_id          Int64,
    strategy_id        LowCardinality(String),
    symbol             LowCardinality(String),
    intent_type        LowCardinality(String),  -- NEW / AMEND / CANCEL / FORCE_FLAT
    side               LowCardinality(String),  -- BUY / SELL
    price_scaled       Int64 CODEC(DoubleDelta, LZ4),
    qty                Int64 CODEC(DoubleDelta, LZ4),
    tif                LowCardinality(String),  -- LIMIT / IOC / FOK / ROD
    target_order_id    String,
    timestamp_ns       Int64 CODEC(DoubleDelta, LZ4),
    source_ts_ns       Int64 CODEC(DoubleDelta, LZ4),
    decision_price     Int64 CODEC(DoubleDelta, LZ4),
    price_type         LowCardinality(String),
    trace_id           String,
    idempotency_key    String,
    ttl_ns             Int64 CODEC(DoubleDelta, LZ4),
    reason             String CODEC(ZSTD(3)),
    ingest_ts          Int64 CODEC(DoubleDelta, LZ4),
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime64(ingest_ts/1e9, 3))
ORDER BY (strategy_id, symbol, timestamp_ns, intent_id)
TTL toDate(toDateTime64(ingest_ts/1e9, 3)) + INTERVAL 365 DAY  -- aligned with hft.orders 365d retention (see 20260302_001_add_ttl_policies.sql)
SETTINGS index_granularity = 8192;

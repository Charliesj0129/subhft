-- src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql
-- Slice D: append-only kill ledger for the alpha-promotion factory.
-- One row per Gate-{A..F} / pre_screen / cluster / manual rejection.
-- Idempotency: kill_id = sha256(alpha_id || ':' || gate || ':' || stable_artifact_hash);
-- duplicate (alpha_id, kill_id) rows MUST be deduped by the writer (kill_ledger.append_kill).
-- This table is MergeTree (NOT ReplacingMergeTree) because the writer-side
-- dedupe via SELECT count() WHERE alpha_id=? AND kill_id=? is the source of
-- truth — operators can audit ledger contents without waiting for a merge.
CREATE TABLE IF NOT EXISTS audit.alpha_kill_ledger (
    kill_id              String                      NOT NULL, -- sha256(alpha_id || ':' || gate || ':' || stable_artifact_hash)
    killed_at            DateTime64(9, 'UTC')        DEFAULT now64(9, 'UTC'),
    alpha_id             String                      NOT NULL,
    gate                 Enum8('A'=1,'B'=2,'C'=3,'D'=4,'E'=5,'F'=6,'pre_screen'=7,'cluster'=8,'manual'=9),
    reason               String                      NOT NULL,
    stable_artifact_hash String                      DEFAULT '', -- sha256 over canonical-json manifest excluding kill_reason/cluster_id
    scorecard_id         String                      DEFAULT '',
    killed_by            String                      DEFAULT 'system'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(killed_at)
ORDER BY (alpha_id, kill_id, killed_at)
TTL killed_at + INTERVAL 365 DAY                                -- aligned with hft.orders / hft.order_intents 365d retention
SETTINGS index_granularity = 8192;

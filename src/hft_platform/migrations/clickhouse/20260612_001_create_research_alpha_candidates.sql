-- src/hft_platform/migrations/clickhouse/20260612_001_create_research_alpha_candidates.sql
-- Alpha Candidate Loop v1 (docs/research/alpha_candidate_loop_v1_spec.md §8).
-- New `research` database: experiment telemetry, NOT governance records
-- (`audit` stays reserved for compliance/kill-ledger rows).
-- Append-only registry of candidate status transitions; current state =
-- argMax(status, inserted_at) per alpha_id. Never UPDATE/DELETE.
-- Up
CREATE DATABASE IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.alpha_candidates (
    alpha_id        String,                          -- sha256(canonical candidate json)[:16]
    inserted_at     DateTime64(9,'UTC') DEFAULT now64(9,'UTC'),
    run_id          String,
    name            String,
    family          LowCardinality(String),
    status          Enum8('NEW'=1,'INVALID'=2,'COMPILED'=3,'EVALUATED'=4,
                          'REJECTED'=5,'WATCHLIST'=6,'PROMOTED'=7),
    death_reason    LowCardinality(String) DEFAULT '',
    hypothesis      String,
    candidate_json  String CODEC(ZSTD(3)),           -- verbatim original
    feature_formulas Array(String),
    signal_formula  String,
    label           String,
    horizon         String,
    expected_sign   Enum8('positive'=1,'negative'=2),
    regime_filter   String DEFAULT '',
    formula_hash    String,
    uses_trade_imbalance UInt8 DEFAULT 0,
    proposed_new_primitives Array(String),
    generation_model String,
    generation_prompt_id String,
    generation_run_id String,
    data_version LowCardinality(String),
    primitive_version LowCardinality(String),
    schema_version LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(inserted_at)
ORDER BY (family, alpha_id, inserted_at);

-- Down
DROP TABLE IF EXISTS research.alpha_candidates;

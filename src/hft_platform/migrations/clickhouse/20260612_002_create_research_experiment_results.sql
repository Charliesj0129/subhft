-- src/hft_platform/migrations/clickhouse/20260612_002_create_research_experiment_results.sql
-- Alpha Candidate Loop v1 (docs/research/alpha_candidate_loop_v1_spec.md §8).
-- One row per alpha × split × version-tuple; result_id is the dedupe key.
-- APPROVED EXTENSION beyond the frozen spec (2026-06-12): four additive
-- maker_* columns record a maker-aware cost view (taifex_maker_qhat_v1,
-- q_hat fill-prob-weighted expected cost). The spec's taker
-- cost_survival_score semantics (taifex_v1) are unchanged; maker columns
-- are recorded alongside and never relax any gate.
-- Up
CREATE DATABASE IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.experiment_results (
    result_id    String,   -- sha256(alpha_id:run_id:split:data_version:evaluator_version:scoring_version)
    inserted_at  DateTime64(9,'UTC') DEFAULT now64(9,'UTC'),
    experiment_id String,
    run_id String,
    alpha_id String,
    family LowCardinality(String),
    split Enum8('train'=1,'validation'=2,'test'=3),
    split_start Date,
    split_end Date,
    day_count UInt16,
    effective_day_count UInt16,
    ic Float64,
    rank_ic Float64,
    ic_tstat Float64,
    sign_consistency Float64,
    bucket_spread_pts Float64,
    bucket_monotonicity Float64,
    horizon_decay_halflife_ms Float64,
    day_stability Float64,
    one_day_concentration Float64,
    regime_ic_in Float64,
    regime_ic_out Float64,
    regime_ic_tight_spread Float64,
    regime_ic_wide_spread Float64,
    regime_stability Float64,
    train_validation_direction_match UInt8,
    validation_test_direction_match UInt8,
    turnover_proxy Float64,
    gross_pts_per_flip Float64,
    required_move_threshold_pts Float64,
    cost_survival_score Float64,
    maker_fill_prob_mean Float64,                 -- extension: mean q_hat over flip events
    maker_required_move_threshold_pts Float64,    -- extension: 2*(comm+tax) + (1-p_fill)*median_spread
    maker_cost_survival_score Float64,            -- extension: gross_pts_per_flip / maker threshold
    latency_0ms_score Float64,
    latency_1ms_score Float64,
    latency_5ms_score Float64,
    latency_10ms_score Float64,
    final_score Float64 DEFAULT 0,
    gates_passed Array(String),
    gates_failed Array(String),
    status LowCardinality(String),
    death_reason LowCardinality(String) DEFAULT '',
    artifact_path String,
    data_version LowCardinality(String),
    primitive_version LowCardinality(String),
    evaluator_version LowCardinality(String),
    scoring_version LowCardinality(String),
    cost_assumption_version LowCardinality(String),
    maker_cost_assumption_version LowCardinality(String) DEFAULT '',  -- extension
    latency_config_version LowCardinality(String),
    generation_model String,
    generation_prompt_id String,
    generation_run_id String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(inserted_at)
ORDER BY (run_id, family, split, alpha_id);

-- Down
DROP TABLE IF EXISTS research.experiment_results;

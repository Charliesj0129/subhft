from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ValidationConfig:
    alpha_id: str
    data_paths: list[str]
    is_oos_split: float = 0.7
    signal_threshold: float = 0.3
    max_position: int = 5
    min_sharpe_oos: float = 0.0
    max_abs_drawdown: float = 0.3
    min_turnover: float = 1e-6
    skip_gate_b_tests: bool = False
    pytest_timeout_s: int = 300
    project_root: str = "."
    experiments_dir: str = "research/experiments"
    latency_profile_id: str = "sim_p95_v2026-02-26"
    local_decision_pipeline_latency_us: int = 250
    submit_ack_latency_ms: float = 36.0
    modify_ack_latency_ms: float = 43.0
    cancel_ack_latency_ms: float = 47.0
    live_uplift_factor: float = 1.5
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    sell_tax_bps: float = 2.0  # TAIFEX securities transaction tax on sells only (bps)
    stat_pvalue_threshold: float = 0.1
    min_stat_tests_pass: int = 2
    stat_correction_method: str = "bh"
    min_stat_tests_bh_pass: int = 1
    enable_walk_forward: bool = True
    wf_n_splits: int = 5
    wf_min_fold_consistency: float = 0.6
    wf_min_fold_sharpe_min: float = -0.5
    enable_param_optimization: bool = True
    opt_signal_threshold_min: float = 0.05
    opt_signal_threshold_max: float = 0.6
    opt_signal_threshold_steps: int = 8
    opt_objective: str = "risk_adjusted"
    opt_max_is_oos_gap: float = 1.0
    opt_min_neighbor_objective_ratio: float = 0.6
    opt_min_deflated_sharpe: float = -0.1
    require_paper_refs: bool = False
    require_paper_index_link: bool = False
    enforce_data_governance: bool = False
    require_data_meta: bool = False
    allowed_data_roots: tuple[str, ...] = (
        "research/data/raw",
        "research/data/interim",
        "research/data/processed",
        "research/data/hbt_multiproduct",
    )
    required_data_provenance_fields: tuple[str, ...] = ()
    data_ul: int = 2
    bootstrap_samples: int = 1000
    use_hft_native: bool = True
    stress_latency_multiplier: float = 1.5
    stress_fee_multiplier: float = 1.5
    min_stress_sharpe_ratio: float = 0.5
    stress_drawdown_limit_multiplier: float = 1.25
    backtest_engine: str = "hftbacktest_v2"
    queue_model: str = "PowerProbQueueModel(3.0)"
    latency_model: str = "IntpOrderLatency"
    exchange_model: str = "NoPartialFillExchange"
    min_queue_survival_rate: float = 0.3
    enforce_latency_profile: bool = False
    gate_c_tier: str = "promotion"


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    """Lightweight screening configuration for pre-Gate-C evaluation."""

    alpha_id: str
    data_paths: list[str]
    is_oos_split: float = 0.7
    signal_threshold: float = 0.3
    max_position: int = 5
    min_ic: float = 0.005
    min_sharpe_oos: float = -0.5
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    latency_profile_id: str = "sim_p95_v2026-02-26"
    local_decision_pipeline_latency_us: int = 250
    submit_ack_latency_ms: float = 36.0
    modify_ack_latency_ms: float = 43.0
    cancel_ack_latency_ms: float = 47.0
    live_uplift_factor: float = 1.5
    backtest_engine: str = "hftbacktest_v2"
    queue_model: str = "PowerProbQueueModel(3.0)"
    latency_model: str = "IntpOrderLatency"
    exchange_model: str = "NoPartialFillExchange"
    min_queue_survival_rate: float = 0.3
    project_root: str = "."
    experiments_dir: str = "research/experiments"


@dataclass(frozen=True)
class GateReport:
    gate: str
    passed: bool
    details: dict[str, Any]


@dataclass(frozen=True)
class ValidationResult:
    alpha_id: str
    passed: bool
    gate_a: GateReport
    gate_b: GateReport
    gate_c: GateReport
    scorecard_path: str
    run_id: str | None
    config_hash: str | None
    experiment_meta_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

"""Fast Gate C — discovery-tier lightweight backtest evaluation.

Runs a reduced Gate C pipeline: single-fold backtest without walk-forward,
parameter optimization, stress testing, or robustness sweeps.
Used when ``gate_c_tier == "discovery"`` to quickly evaluate alphas
before investing in full promotion-tier Gate C.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from hft_platform.alpha._validation_helpers import _ensure_project_root_on_path
from hft_platform.alpha._validation_types import GateReport, ValidationConfig

_log = structlog.get_logger(__name__)


def run_fast_gate_c(
    alpha: Any,
    config: ValidationConfig,
    root: Path,
    resolved_data_paths: list[str],
    experiments_base: Path,
) -> tuple[GateReport, str, str, str, str]:
    """Run discovery-tier Gate C (single-fold, no optimization).

    Returns the same tuple shape as ``run_gate_c`` for compatibility:
        (GateReport, scorecard_path, run_id, config_hash, experiment_meta_path)
    """
    _ensure_project_root_on_path(root)

    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig
    from research.registry.scorecard import compute_scorecard

    for dp in resolved_data_paths:
        ensure_hftbt_npz(dp)

    backtest_cfg = BacktestConfig(
        data_paths=resolved_data_paths,
        is_oos_split=float(config.is_oos_split),
        signal_threshold=float(config.signal_threshold),
        max_position=int(config.max_position),
        maker_fee_bps=float(config.maker_fee_bps),
        taker_fee_bps=float(config.taker_fee_bps),
        latency_profile_id=str(config.latency_profile_id),
        local_decision_pipeline_latency_us=int(config.local_decision_pipeline_latency_us),
        submit_ack_latency_ms=float(config.submit_ack_latency_ms),
        modify_ack_latency_ms=float(config.modify_ack_latency_ms),
        cancel_ack_latency_ms=float(config.cancel_ack_latency_ms),
        live_uplift_factor=float(config.live_uplift_factor),
        backtest_engine=str(config.backtest_engine),
        queue_model=str(config.queue_model),
        latency_model=str(config.latency_model),
        exchange_model=str(config.exchange_model),
        min_queue_survival_rate=float(config.min_queue_survival_rate),
    )

    runner: Any = HftNativeRunner(alpha, backtest_cfg)
    result = runner.run()

    tracker = ExperimentTracker(base_dir=experiments_base)
    pool_signals = tracker.latest_signals_by_alpha()
    pool_signals = {k: v for k, v in pool_signals.items() if k != alpha.manifest.alpha_id}

    scorecard = compute_scorecard(
        {
            "signals": result.signals,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "regime_metrics": result.regime_metrics,
            "capacity_estimate": result.capacity_estimate,
            "latency_profile": result.latency_profile,
        },
        pool_signals=pool_signals,
    )

    run_id = tracker.record_run(alpha.manifest.alpha_id, scorecard)
    scorecard_path = str(tracker.scorecard_path(run_id))

    passed = float(result.sharpe_oos) >= float(config.min_sharpe_oos)
    details: dict[str, Any] = {
        "tier": "discovery",
        "sharpe_oos": float(result.sharpe_oos),
        "ic_mean": float(result.ic_mean),
        "max_drawdown": float(result.max_drawdown),
        "correlation_pool_max": float(getattr(scorecard, "correlation_pool_max", 0.0)),
    }

    _log.info(
        "fast_gate_c_complete",
        alpha_id=alpha.manifest.alpha_id,
        passed=passed,
        sharpe_oos=details["sharpe_oos"],
    )

    report = GateReport(gate="C_discovery", passed=passed, details=details)
    return report, scorecard_path, run_id, "", ""

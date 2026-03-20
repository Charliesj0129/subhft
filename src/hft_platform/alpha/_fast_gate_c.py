"""Fast Gate C — discovery-tier lightweight backtest evaluation.

Runs a reduced Gate C pipeline: single-fold backtest without walk-forward,
parameter optimization, stress testing, or robustness sweeps.
Used when ``gate_c_tier == "discovery"`` to quickly evaluate alphas
before investing in full promotion-tier Gate C.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

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
    latest_pool_signals = tracker.latest_signals_by_alpha()
    pool_signals: dict[str, Sequence[float]] = {
        k: tuple(float(x) for x in v.tolist())
        for k, v in latest_pool_signals.items()
        if k != alpha.manifest.alpha_id
    }

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

    run_id = str(result.run_id)
    scorecard_path = experiments_base / "runs" / run_id / "scorecard.json"
    meta_path = tracker.log_run(
        run_id=run_id,
        alpha_id=str(alpha.manifest.alpha_id),
        config_hash=str(result.config_hash),
        data_paths=[str(p) for p in resolved_data_paths],
        metrics={
            "sharpe_is": float(result.sharpe_is),
            "sharpe_oos": float(result.sharpe_oos),
            "ic_mean": float(result.ic_mean),
            "ic_std": float(result.ic_std),
            "turnover": float(result.turnover),
            "max_drawdown": float(result.max_drawdown),
            "capacity_estimate": float(result.capacity_estimate),
        },
        gate_status={"gate_c_discovery": bool(float(result.sharpe_oos) >= float(config.min_sharpe_oos))},
        scorecard_payload=scorecard.to_dict(),
        backtest_report_payload={
            "run_id": run_id,
            "config_hash": str(result.config_hash),
            "alpha_id": str(alpha.manifest.alpha_id),
            "tier": "discovery",
        },
        signals=result.signals,
        equity=result.equity_curve,
    )

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
    return report, str(scorecard_path), run_id, str(result.config_hash), str(meta_path)

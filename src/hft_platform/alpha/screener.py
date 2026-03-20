"""Fast Signal Screener — lightweight pre-Gate-C evaluation.

Runs a single-fold backtest (no walk-forward, no param optimization,
no stress test, no robustness sweep) and computes IC + Sharpe +
max_drawdown + pool correlation.  Designed to quickly kill unpromising
alphas before investing ~7 min in full Gate C.

Usage:
    hft alpha screen --alpha-id X --data Y
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.alpha._validation_types import ScreenConfig

logger = get_logger("alpha_screener")

_DEFAULT_MIN_IC = 0.005
_DEFAULT_MIN_SHARPE = -0.5


@dataclass(frozen=True)
class ScreenResult:
    """Lightweight screening result."""

    screen_passed: bool
    sharpe_oos: float
    ic_mean: float
    max_drawdown: float
    correlation_pool_max: float
    runtime_seconds: float
    kill_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_screen(config: ScreenConfig) -> ScreenResult:
    """Run lightweight pre-Gate-C screening."""
    t0 = time.monotonic()

    from hft_platform.alpha._validation_helpers import (
        _ensure_project_root_on_path,
        _resolve_first_data_meta_path,
    )

    root = Path(config.project_root).resolve()
    _ensure_project_root_on_path(root)

    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig
    from research.registry.alpha_registry import AlphaRegistry
    from research.registry.scorecard import compute_scorecard

    registry = AlphaRegistry()
    loaded = registry.discover("research/alphas")
    alpha = loaded.get(config.alpha_id)
    if alpha is None:
        raise ValueError(f"Alpha {config.alpha_id!r} not found in research/alphas")

    resolved_data_paths = [str(Path(p).resolve()) for p in config.data_paths]
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

    experiments_base = Path(config.experiments_dir)
    tracker = ExperimentTracker(base_dir=experiments_base)
    latest_signals = getattr(tracker, "latest_signals_by_alpha", None)
    pool_signals = latest_signals() if callable(latest_signals) else {}
    pool_signals = {k: v for k, v in dict(pool_signals).items() if str(k) != str(config.alpha_id)}

    data_meta_path = _resolve_first_data_meta_path(resolved_data_paths)
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
        data_meta_path=data_meta_path,
    )

    sharpe_oos = float(result.sharpe_oos)
    ic_mean = float(result.ic_mean)
    max_drawdown = float(result.max_drawdown)
    corr_max = float(scorecard.correlation_pool_max or 0.0)
    elapsed = time.monotonic() - t0

    min_ic = float(config.min_ic)
    min_sharpe = float(config.min_sharpe_oos)
    kill_reason: str | None = None
    passed = True

    if abs(ic_mean) < min_ic:
        passed = False
        kill_reason = f"IC too low: |{ic_mean:.6f}| < {min_ic}"
    elif sharpe_oos < min_sharpe:
        passed = False
        kill_reason = f"Sharpe OOS too low: {sharpe_oos:.4f} < {min_sharpe}"

    logger.info(
        "Screen complete",
        alpha_id=config.alpha_id,
        passed=passed,
        sharpe_oos=sharpe_oos,
        ic_mean=ic_mean,
        max_drawdown=max_drawdown,
        correlation_pool_max=corr_max,
        runtime_s=round(elapsed, 2),
        kill_reason=kill_reason,
    )

    return ScreenResult(
        screen_passed=passed,
        sharpe_oos=sharpe_oos,
        ic_mean=ic_mean,
        max_drawdown=max_drawdown,
        correlation_pool_max=corr_max,
        runtime_seconds=round(elapsed, 3),
        kill_reason=kill_reason,
    )

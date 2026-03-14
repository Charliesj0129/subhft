"""Bayesian optimization tool for multi-dimensional parameter search.

Uses Optuna TPE sampler to explore alpha parameter spaces more efficiently
than grid search, with deflated Sharpe correction for multiple-testing bias.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


def _ensure_project_root_on_path() -> None:
    root = Path(__file__).resolve().parents[2]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


_ensure_project_root_on_path()


@dataclass(frozen=True)
class BayesianOptConfig:
    """Configuration for Bayesian optimization of alpha parameters."""

    alpha_id: str
    data_paths: list[str]
    n_trials: int = 30
    param_space: dict[str, tuple[float, float, bool]] = None  # type: ignore[assignment]
    # name -> (lo, hi, log_scale)
    objective: str = "risk_adjusted"  # matches validation.py's opt_objective
    n_startup_trials: int = 10
    seed: int | None = None
    latency_profile_id: str = "shioaji_sim_p95_v2026-03-04"
    # backtest config defaults
    is_oos_split: float = 0.7
    max_position: int = 5
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2

    def __post_init__(self) -> None:
        # Frozen dataclass workaround: use object.__setattr__ for default mutable field
        if self.param_space is None:
            object.__setattr__(
                self,
                "param_space",
                {"signal_threshold": (0.01, 0.60, False)},
            )


@dataclass(frozen=True)
class BayesianOptResult:
    """Structured result from Bayesian optimization run."""

    best_params: dict[str, float]
    best_objective: float
    deflated_sharpe: float
    n_trials: int
    trials: list[dict[str, Any]]
    param_importance: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_params": dict(self.best_params),
            "best_objective": float(self.best_objective),
            "deflated_sharpe": float(self.deflated_sharpe),
            "n_trials": int(self.n_trials),
            "trials": list(self.trials),
            "param_importance": dict(self.param_importance),
        }


@dataclass(frozen=True)
class MultiObjectiveResult:
    """Structured result from multi-objective Bayesian optimization."""

    pareto_front: list[dict[str, Any]]
    n_trials: int
    neighbor_consistency: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pareto_front": list(self.pareto_front),
            "n_trials": int(self.n_trials),
            "neighbor_consistency": float(self.neighbor_consistency),
        }


def _optimization_objective(
    sharpe_oos: float,
    max_drawdown: float,
    turnover: float,
    objective: str,
) -> float:
    """Compute risk-adjusted objective — mirrors validation.py logic."""
    mode = objective.strip().lower()
    if mode == "sharpe_oos":
        return float(sharpe_oos)
    if mode == "ic_first":
        return float(sharpe_oos) - 0.1 * abs(float(turnover))
    # Default: risk-adjusted objective.
    drawdown_penalty = max(0.0, abs(float(max_drawdown)) - 0.10) * 2.0
    turnover_penalty = max(0.0, float(turnover) - 1.0) * 0.25
    return float(sharpe_oos) - float(drawdown_penalty) - float(turnover_penalty)


def run_bayesian_opt(config: BayesianOptConfig) -> BayesianOptResult:
    """Run Bayesian optimization over alpha parameter space.

    Discovers the alpha via AlphaRegistry, sweeps parameters using Optuna TPE,
    and returns the best configuration with deflated Sharpe correction.
    """
    import optuna

    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig
    from research.registry.alpha_registry import AlphaRegistry
    from research.tools.latency_profiles import load_latency_profile

    # --- Load latency profile ---
    latency = load_latency_profile(config.latency_profile_id)

    # --- Discover alpha ---
    project_root = Path(__file__).resolve().parents[2]
    alphas_dir = project_root / "research" / "alphas"
    registry = AlphaRegistry()
    loaded = registry.discover(alphas_dir)
    if config.alpha_id not in loaded:
        available = sorted(loaded.keys())
        raise ValueError(
            f"Alpha '{config.alpha_id}' not found in research/alphas/. "
            f"Available: {available}"
        )

    # --- Resolve data paths and ensure hftbt format ---
    resolved_paths: list[str] = []
    for p in config.data_paths:
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = (project_root / p).resolve()
        resolved_paths.append(str(candidate))

    for dp in resolved_paths:
        ensure_hftbt_npz(dp)

    # --- Build base backtest config ---
    base_backtest_cfg = BacktestConfig(
        data_paths=resolved_paths,
        is_oos_split=float(config.is_oos_split),
        max_position=int(config.max_position),
        maker_fee_bps=float(config.maker_fee_bps),
        taker_fee_bps=float(config.taker_fee_bps),
        latency_profile_id=str(config.latency_profile_id),
        submit_ack_latency_ms=float(latency["submit_ack_latency_ms"]),
        modify_ack_latency_ms=float(latency["modify_ack_latency_ms"]),
        cancel_ack_latency_ms=float(latency["cancel_ack_latency_ms"]),
        local_decision_pipeline_latency_us=int(latency["local_decision_pipeline_latency_us"]),
    )

    param_space = dict(config.param_space)

    # --- Silence Optuna's internal logging ---
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    # --- Create study ---
    sampler = optuna.samplers.TPESampler(
        n_startup_trials=min(config.n_startup_trials, config.n_trials),
        seed=config.seed,
    )
    study = optuna.create_study(direction="maximize", sampler=sampler)

    trial_records: list[dict[str, Any]] = []

    def objective_fn(trial: optuna.Trial) -> float:
        # Suggest parameters
        params: dict[str, float] = {}
        for name, (lo, hi, log_scale) in param_space.items():
            params[name] = trial.suggest_float(name, lo, hi, log=log_scale)

        # Reconstruct alpha instance (fresh state per trial)
        alpha_cls = type(loaded[config.alpha_id])
        alpha_instance = alpha_cls()

        # Build trial-specific backtest config
        trial_cfg = base_backtest_cfg
        if "signal_threshold" in params:
            trial_cfg = replace(trial_cfg, signal_threshold=params["signal_threshold"])

        runner = HftNativeRunner(alpha_instance, trial_cfg)
        result = runner.run()

        obj_value = _optimization_objective(
            float(result.sharpe_oos),
            float(result.max_drawdown),
            float(result.turnover),
            config.objective,
        )

        trial_record = {
            "trial_number": trial.number,
            "params": dict(params),
            "sharpe_oos": float(result.sharpe_oos),
            "sharpe_is": float(result.sharpe_is),
            "max_drawdown": float(result.max_drawdown),
            "turnover": float(result.turnover),
            "objective": float(obj_value),
        }
        trial_records.append(trial_record)

        return float(obj_value)

    study.optimize(objective_fn, n_trials=config.n_trials)

    # --- Extract results ---
    best_trial = study.best_trial
    best_params = dict(best_trial.params)
    best_objective = float(best_trial.value)

    # --- Compute deflated Sharpe ---
    # Penalty for multiple testing: best_sharpe - sqrt(2 * log(n_trials) / n_oos)
    n_oos_fraction = 1.0 - config.is_oos_split
    # Estimate n_oos from a representative data file
    n_oos = 100  # fallback
    try:
        sample_path = resolved_paths[0] if resolved_paths else None
        if sample_path:
            raw = np.load(sample_path, allow_pickle=False)
            if isinstance(raw, np.lib.npyio.NpzFile):
                arr = np.asarray(raw["data"])
                raw.close()
            else:
                arr = np.asarray(raw)
            n_total = len(arr)
            n_oos = max(1, int(n_total * n_oos_fraction))
    except Exception:
        pass  # use fallback

    selection_penalty = math.sqrt(2.0 * math.log(max(1, config.n_trials)) / max(1, n_oos))
    deflated_sharpe = best_objective - selection_penalty

    # --- Parameter importance ---
    try:
        importance = optuna.importance.get_param_importances(study)
    except Exception:
        importance = {}

    logger.info(
        "bayesian_opt_complete",
        alpha_id=config.alpha_id,
        n_trials=config.n_trials,
        best_objective=best_objective,
        deflated_sharpe=deflated_sharpe,
        best_params=best_params,
    )

    return BayesianOptResult(
        best_params=best_params,
        best_objective=best_objective,
        deflated_sharpe=deflated_sharpe,
        n_trials=config.n_trials,
        trials=trial_records,
        param_importance=dict(importance),
    )

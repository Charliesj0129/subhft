from __future__ import annotations

import concurrent.futures
import multiprocessing as mp
import os
import pickle
from dataclasses import replace
from typing import Any

import numpy as np

from hft_platform.alpha._validation_types import ValidationConfig


def _build_grid_row(
    result: Any,
    threshold: float,
    objective_mode: str,
) -> dict[str, Any]:
    """Build a standardized grid row dict from a backtest result."""
    objective = _optimization_objective(
        float(result.sharpe_oos),
        float(result.max_drawdown),
        float(result.turnover),
        objective_mode,
    )
    return {
        "signal_threshold": threshold,
        "sharpe_is": float(result.sharpe_is),
        "sharpe_oos": float(result.sharpe_oos),
        "max_drawdown": float(result.max_drawdown),
        "turnover": float(result.turnover),
        "objective": float(objective),
        "run_id": str(result.run_id),
        "config_hash": str(result.config_hash),
    }


def _run_single_grid_point(
    alpha: Any,
    runner_cls: Any,
    base_cfg: Any,
    threshold: float,
    objective_mode: str,
) -> dict[str, Any]:
    """Run a single backtest grid point. Top-level for ProcessPoolExecutor pickling."""
    cfg = replace(base_cfg, signal_threshold=threshold)
    result = runner_cls(alpha, cfg).run()
    return _build_grid_row(result, threshold, objective_mode)


def _submit_grid_points(
    *,
    alpha: Any,
    runner_cls: Any,
    base_cfg: Any,
    thresholds: list[float],
    objective_mode: str,
    max_workers: int,
) -> list[dict[str, Any]]:
    """Submit grid points to ProcessPoolExecutor, falling back to sequential on pickle errors.

    Falls back to sequential execution when alpha/runner_cls are not picklable
    (e.g. locally-defined classes in tests or lambdas).
    """
    if max_workers <= 1:
        return [_run_single_grid_point(alpha, runner_cls, base_cfg, t, objective_mode) for t in thresholds]

    # Pre-check picklability to avoid spawning processes that will fail
    try:
        pickle.dumps((alpha, runner_cls, base_cfg))
    except (pickle.PicklingError, AttributeError, TypeError):
        return [_run_single_grid_point(alpha, runner_cls, base_cfg, t, objective_mode) for t in thresholds]

    executor_kwargs: dict[str, Any] = {"max_workers": max_workers}
    if max_workers > 1:
        # Python 3.12 warns on forking multi-threaded processes. Use an
        # explicit spawn context so parallel optimization stays warning-free.
        executor_kwargs["mp_context"] = mp.get_context("spawn")

    with concurrent.futures.ProcessPoolExecutor(**executor_kwargs) as executor:
        futures = [
            executor.submit(
                _run_single_grid_point,
                alpha,
                runner_cls,
                base_cfg,
                t,
                objective_mode,
            )
            for t in thresholds
        ]
        return [f.result() for f in futures]


def _run_grid_parallel(
    *,
    alpha: Any,
    runner_cls: Any,
    base_cfg: Any,
    base_result: Any,
    base_threshold: float,
    grid: np.ndarray,
    objective_mode: str,
) -> list[dict[str, Any]]:
    """Run grid search in parallel using ProcessPoolExecutor.

    Reuses the already-computed base_result for the base threshold point.
    Non-base points are submitted to a process pool.

    Environment variable HFT_OPT_WORKERS controls max parallelism:
      - Default: min(len(grid), min(cpu_count, 8))
      - Set HFT_OPT_WORKERS=1 to force sequential execution
    """
    base_row = _build_grid_row(base_result, base_threshold, objective_mode)

    # Separate base vs non-base thresholds
    non_base_thresholds = [float(t) for t in grid if abs(float(t) - base_threshold) >= 1e-12]

    if not non_base_thresholds:
        return [base_row]

    # HFT_OPT_WORKERS: max parallel workers for parameter grid search.
    # Default: min(grid_size, min(cpu_count, 8)). Set to 1 for sequential.
    max_workers = min(
        len(non_base_thresholds),
        int(os.environ.get("HFT_OPT_WORKERS", min(os.cpu_count() or 4, 8))),
    )

    non_base_rows = _submit_grid_points(
        alpha=alpha,
        runner_cls=runner_cls,
        base_cfg=base_cfg,
        thresholds=non_base_thresholds,
        objective_mode=objective_mode,
        max_workers=max_workers,
    )

    # Combine and sort by threshold for deterministic ordering
    rows = [base_row] + non_base_rows
    rows.sort(key=lambda r: float(r["signal_threshold"]))
    return rows


def _optimize_parameters(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    config: ValidationConfig,
    runner_cls: Any,
) -> dict[str, Any]:
    if not bool(config.enable_param_optimization):
        return {
            "enabled": False,
            "passed": True,
            "objective": str(config.opt_objective),
            "selected_signal_threshold": float(base_cfg.signal_threshold),
            "selected_row": {
                "signal_threshold": float(base_cfg.signal_threshold),
                "sharpe_is": float(base_result.sharpe_is),
                "sharpe_oos": float(base_result.sharpe_oos),
                "max_drawdown": float(base_result.max_drawdown),
                "turnover": float(base_result.turnover),
                "objective": _optimization_objective(
                    float(base_result.sharpe_oos),
                    float(base_result.max_drawdown),
                    float(base_result.turnover),
                    str(config.opt_objective),
                ),
            },
            "grid": [],
            "trials": [],
            "risks": {},
        }

    lo = max(1e-6, float(config.opt_signal_threshold_min))
    hi = max(lo, float(config.opt_signal_threshold_max))
    steps = max(2, int(config.opt_signal_threshold_steps))
    grid = np.linspace(lo, hi, num=steps, dtype=np.float64)
    base_threshold = float(base_cfg.signal_threshold)
    grid = np.unique(np.append(grid, np.asarray([base_threshold], dtype=np.float64)))
    grid.sort()

    rows = _run_grid_parallel(
        alpha=alpha,
        runner_cls=runner_cls,
        base_cfg=base_cfg,
        base_result=base_result,
        base_threshold=base_threshold,
        grid=grid,
        objective_mode=str(config.opt_objective),
    )

    if not rows:
        return {
            "enabled": True,
            "passed": False,
            "objective": str(config.opt_objective),
            "selected_signal_threshold": float(base_threshold),
            "selected_row": None,
            "grid": [],
            "trials": [],
            "risks": {"no_trials": True},
        }

    ranked = sorted(rows, key=lambda row: float(row["objective"]), reverse=True)
    best = ranked[0]
    best_threshold = float(best["signal_threshold"])
    idx = min(range(len(rows)), key=lambda i: abs(float(rows[i]["signal_threshold"]) - best_threshold))

    neighbors: list[dict[str, Any]] = []
    if idx - 1 >= 0:
        neighbors.append(rows[idx - 1])
    if idx + 1 < len(rows):
        neighbors.append(rows[idx + 1])

    best_obj = float(best["objective"])
    neighbor_objs = np.asarray([float(row["objective"]) for row in neighbors], dtype=np.float64)
    neighbor_sharpes = np.asarray([float(row["sharpe_oos"]) for row in neighbors], dtype=np.float64)

    boundary_risk = idx in {0, len(rows) - 1}
    overfit_gap = float(best["sharpe_is"]) - float(best["sharpe_oos"])
    overfit_gap_risk = overfit_gap > float(config.opt_max_is_oos_gap)

    neighbor_ratio = 1.0
    if neighbor_objs.size and best_obj != 0.0:
        neighbor_ratio = float(np.median(neighbor_objs) / best_obj)
    plateau_risk = bool(
        best_obj > 0.0 and neighbor_objs.size and neighbor_ratio < float(config.opt_min_neighbor_objective_ratio)
    )
    sign_flip_risk = bool(float(best["sharpe_oos"]) > 0.0 and neighbor_sharpes.size and np.any(neighbor_sharpes <= 0.0))

    oos_len = max(2, int((1.0 - float(config.is_oos_split)) * float(base_result.equity_curve.size)))
    n_trials = max(1, len(rows))
    selection_penalty = float(np.sqrt(2.0 * np.log(float(n_trials)) / float(oos_len)))
    deflated_sharpe = float(best["sharpe_oos"]) - selection_penalty
    selection_bias_risk = deflated_sharpe < float(config.opt_min_deflated_sharpe)

    risks = {
        "boundary_risk": bool(boundary_risk),
        "overfit_gap_risk": bool(overfit_gap_risk),
        "plateau_risk": bool(plateau_risk),
        "sign_flip_risk": bool(sign_flip_risk),
        "selection_bias_risk": bool(selection_bias_risk),
    }
    passed = not any(risks.values())
    return {
        "enabled": True,
        "passed": bool(passed),
        "objective": str(config.opt_objective),
        "selected_signal_threshold": float(best_threshold),
        "selected_row": best,
        "base_signal_threshold": float(base_threshold),
        "grid": [float(v) for v in grid],
        "trials": rows,
        "top_k": ranked[: min(3, len(ranked))],
        "deflated_sharpe": float(deflated_sharpe),
        "selection_penalty": float(selection_penalty),
        "neighbor_objective_ratio": float(neighbor_ratio),
        "risks": risks,
    }


def _optimization_objective(
    sharpe_oos: float,
    max_drawdown: float,
    turnover: float,
    objective: str,
) -> float:
    mode = objective.strip().lower()
    if mode == "sharpe_oos":
        return float(sharpe_oos)
    if mode == "ic_first":
        # Fallback objective when IC-first mode is requested but IC is unavailable in this stage.
        return float(sharpe_oos) - 0.1 * abs(float(turnover))
    # Default: risk-adjusted objective.
    drawdown_penalty = max(0.0, abs(float(max_drawdown)) - 0.10) * 2.0
    turnover_penalty = max(0.0, float(turnover) - 1.0) * 0.25
    return float(sharpe_oos) - float(drawdown_penalty) - float(turnover_penalty)


def _evaluate_stress_backtest(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    config: ValidationConfig,
    runner_cls: Any,
) -> dict[str, Any]:
    latency_mult = max(1.0, float(config.stress_latency_multiplier))
    fee_mult = max(1.0, float(config.stress_fee_multiplier))
    stress_cfg = replace(
        base_cfg,
        maker_fee_bps=float(base_cfg.maker_fee_bps) * fee_mult,
        taker_fee_bps=float(base_cfg.taker_fee_bps) * fee_mult,
        submit_ack_latency_ms=float(base_cfg.submit_ack_latency_ms) * latency_mult,
        modify_ack_latency_ms=float(base_cfg.modify_ack_latency_ms) * latency_mult,
        cancel_ack_latency_ms=float(base_cfg.cancel_ack_latency_ms) * latency_mult,
        live_uplift_factor=float(base_cfg.live_uplift_factor) * latency_mult,
        latency_profile_id=f"{base_cfg.latency_profile_id}_stress",
    )
    stress_result = runner_cls(alpha, stress_cfg).run()

    base_sharpe = float(base_result.sharpe_oos)
    stress_sharpe = float(stress_result.sharpe_oos)
    sharpe_ratio = (stress_sharpe / base_sharpe) if abs(base_sharpe) > 1e-12 else None
    if base_sharpe > 0.0:
        sharpe_pass = bool(stress_sharpe >= (base_sharpe * float(config.min_stress_sharpe_ratio)))
    else:
        sharpe_pass = bool(stress_sharpe >= float(config.min_sharpe_oos))

    stress_dd_limit = -abs(float(config.max_abs_drawdown)) * max(1.0, float(config.stress_drawdown_limit_multiplier))
    drawdown_pass = bool(float(stress_result.max_drawdown) >= stress_dd_limit)
    passed = sharpe_pass and drawdown_pass
    return {
        "passed": passed,
        "stress_run_id": str(stress_result.run_id),
        "stress_config_hash": str(stress_result.config_hash),
        "stress_sharpe_oos": stress_sharpe,
        "base_sharpe_oos": base_sharpe,
        "stress_sharpe_ratio_vs_base": sharpe_ratio,
        "stress_max_drawdown": float(stress_result.max_drawdown),
        "stress_drawdown_limit": stress_dd_limit,
        "checks": {
            "sharpe_resilience": sharpe_pass,
            "drawdown_limit": drawdown_pass,
        },
        "multipliers": {
            "latency": latency_mult,
            "fees": fee_mult,
        },
    }


def _evaluate_parameter_robustness(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    runner_cls: Any,
) -> dict[str, Any]:
    ratios = (0.8, 1.0, 1.2)
    rows: list[dict[str, Any]] = []
    for ratio in ratios:
        if abs(ratio - 1.0) < 1e-12:
            result = base_result
            threshold = float(base_cfg.signal_threshold)
        else:
            threshold = max(1e-6, float(base_cfg.signal_threshold) * float(ratio))
            cfg = replace(base_cfg, signal_threshold=threshold)
            result = runner_cls(alpha, cfg).run()
        rows.append(
            {
                "ratio": float(ratio),
                "signal_threshold": float(threshold),
                "sharpe_oos": float(result.sharpe_oos),
                "max_drawdown": float(result.max_drawdown),
                "turnover": float(result.turnover),
            }
        )

    base = next((row for row in rows if abs(float(row["ratio"]) - 1.0) < 1e-12), rows[0])
    neighbors = [row for row in rows if abs(float(row["ratio"]) - 1.0) > 1e-12]
    neighbor_sharpes = np.asarray([float(row["sharpe_oos"]) for row in neighbors], dtype=np.float64)
    neighbor_turnovers = np.asarray([float(row["turnover"]) for row in neighbors], dtype=np.float64)
    neighbor_drawdowns = np.asarray([float(row["max_drawdown"]) for row in neighbors], dtype=np.float64)

    base_sharpe = float(base["sharpe_oos"])
    base_turnover = float(base["turnover"])
    base_drawdown = float(base["max_drawdown"])
    median_neighbor_sharpe = float(np.median(neighbor_sharpes)) if neighbor_sharpes.size else float("nan")
    cliff_limit = max(0.25, abs(base_sharpe) * 0.6)

    cliff_risk = bool(
        base_sharpe > 0.0
        and np.isfinite(median_neighbor_sharpe)
        and (base_sharpe - median_neighbor_sharpe) > cliff_limit
    )
    sign_flip_risk = bool(base_sharpe > 0.0 and neighbor_sharpes.size and np.any(neighbor_sharpes <= 0.0))
    turnover_spike_risk = bool(
        base_turnover > 0.0
        and neighbor_turnovers.size
        and np.any(neighbor_turnovers > max(base_turnover * 2.0, base_turnover + 0.5))
    )
    drawdown_jump_risk = bool(
        neighbor_drawdowns.size
        and np.any(np.abs(neighbor_drawdowns) > max(abs(base_drawdown) * 1.5, abs(base_drawdown) + 0.05))
    )
    risks = {
        "cliff_risk": cliff_risk,
        "sign_flip_risk": sign_flip_risk,
        "turnover_spike_risk": turnover_spike_risk,
        "drawdown_jump_risk": drawdown_jump_risk,
    }
    passed = not any(risks.values())
    return {
        "passed": passed,
        "ratios": list(ratios),
        "sweep": rows,
        "median_neighbor_sharpe": median_neighbor_sharpe if np.isfinite(median_neighbor_sharpe) else None,
        "risks": risks,
    }

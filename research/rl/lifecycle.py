from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.promotion import PromotionConfig, PromotionResult, promote_alpha
from research.registry.alpha_registry import AlphaRegistry
from research.rl.alpha_adapter import RLAlphaAdapter, RLAlphaConfig


@dataclass(frozen=True)
class RLRunConfig:
    alpha_id: str
    model_path: str
    feature_fields: tuple[str, ...]
    params: Mapping[str, Any]
    data_paths: tuple[str, ...] = ()
    owner: str = "rl"


def register_rl_alpha(
    *,
    registry: AlphaRegistry | None,
    config: RLAlphaConfig,
    predictor=None,
) -> RLAlphaAdapter:
    adapter = RLAlphaAdapter(config=config, predictor=predictor)
    target = registry or AlphaRegistry()
    target.register(adapter)
    return adapter


def log_rl_run(
    *,
    run_config: RLRunConfig,
    rewards: Sequence[float],
    signals: Sequence[float],
    equity_curve: Sequence[float] | None = None,
    base_dir: str = "research/experiments",
    run_id: str | None = None,
) -> str:
    rewards_arr = np.asarray(rewards, dtype=np.float64).reshape(-1)
    signals_arr = np.asarray(signals, dtype=np.float64).reshape(-1)
    if rewards_arr.size == 0:
        raise ValueError("rewards must not be empty")
    if signals_arr.size == 0:
        raise ValueError("signals must not be empty")

    if equity_curve is None:
        eq = np.cumsum(rewards_arr, dtype=np.float64) + 1_000_000.0
    else:
        eq = np.asarray(equity_curve, dtype=np.float64).reshape(-1)
        if eq.size == 0:
            eq = np.cumsum(rewards_arr, dtype=np.float64) + 1_000_000.0

    tracker = ExperimentTracker(base_dir=base_dir)
    rid = run_id or str(uuid.uuid4())
    cfg_hash = _hash_config(run_config)
    sharpe = _reward_sharpe(rewards_arr)
    scorecard_payload = {
        "sharpe_oos": sharpe,
        "max_drawdown": _max_drawdown(eq),
        "turnover": float(np.mean(np.abs(np.diff(signals_arr, prepend=signals_arr[0])))),
        "correlation_pool_max": None,
        "capacity_estimate": None,
    }
    report_payload = {
        "gate": "RL",
        "passed": True,
        "details": {
            "episode_reward_mean": float(np.mean(rewards_arr)),
            "episode_reward_std": float(np.std(rewards_arr)),
            "steps": int(rewards_arr.size),
            "model_path": run_config.model_path,
            "owner": run_config.owner,
        },
    }

    meta_path = tracker.log_run(
        run_id=rid,
        alpha_id=run_config.alpha_id,
        config_hash=cfg_hash,
        data_paths=list(run_config.data_paths),
        metrics={
            "episode_reward_mean": float(np.mean(rewards_arr)),
            "episode_reward_std": float(np.std(rewards_arr)),
            "reward_sharpe": sharpe,
            "max_drawdown": _max_drawdown(eq),
        },
        gate_status={"rl_run": True},
        scorecard_payload=scorecard_payload,
        backtest_report_payload=report_payload,
        signals=signals_arr,
        equity=eq,
    )
    return str(meta_path)


def promote_latest_rl_run(
    *,
    alpha_id: str,
    owner: str,
    base_dir: str = "research/experiments",
    project_root: str = ".",
    shadow_sessions: int = 0,
    min_shadow_sessions: int = 5,
    drift_alerts: int = 0,
    execution_reject_rate: float = 0.0,
    force: bool = False,
) -> PromotionResult:
    tracker = ExperimentTracker(base_dir=base_dir)
    rows = tracker.list_runs(alpha_id=alpha_id)
    if not rows:
        raise ValueError(f"No experiment runs found for alpha_id={alpha_id}")

    latest = rows[0]
    if not latest.scorecard_path:
        raise ValueError(f"Latest run for alpha_id={alpha_id} has no scorecard path")

    cfg = PromotionConfig(
        alpha_id=alpha_id,
        owner=owner,
        project_root=project_root,
        scorecard_path=latest.scorecard_path,
        shadow_sessions=int(shadow_sessions),
        min_shadow_sessions=int(min_shadow_sessions),
        drift_alerts=int(drift_alerts),
        execution_reject_rate=float(execution_reject_rate),
        force=bool(force),
    )
    return promote_alpha(cfg)


def _hash_config(config: RLRunConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _reward_sharpe(rewards: np.ndarray) -> float:
    sigma = float(np.std(rewards))
    if sigma <= 1e-12:
        return 0.0
    return float(np.mean(rewards) / sigma * np.sqrt(252.0))


def _max_drawdown(equity: np.ndarray) -> float:
    curve = np.asarray(equity, dtype=np.float64)
    if curve.size < 2:
        return 0.0
    peak = np.maximum.accumulate(curve)
    dd = np.divide(curve - peak, peak, out=np.zeros_like(curve), where=np.abs(peak) > 1e-12)
    return float(np.min(dd))

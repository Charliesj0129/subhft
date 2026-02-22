from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker


@dataclass(frozen=True)
class PoolOptimizationResult:
    method: str
    alpha_ids: tuple[str, ...]
    weights: dict[str, float]
    returns_used: bool
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "alpha_ids": list(self.alpha_ids),
            "weights": self.weights,
            "returns_used": self.returns_used,
            "diagnostics": self.diagnostics,
        }


def compute_pool_matrix(
    *,
    base_dir: str = "research/experiments",
    sample_step: int = 1,
) -> dict[str, Any]:
    tracker = ExperimentTracker(base_dir=base_dir)
    signals = tracker.latest_signals_by_alpha()
    return compute_correlation_payload(signals=signals, sample_step=sample_step)


def compute_correlation_payload(
    *,
    signals: Mapping[str, Any],
    sample_step: int = 1,
) -> dict[str, Any]:
    alpha_ids, data = _aligned_signal_matrix(signals, sample_step=max(1, int(sample_step)))
    if not alpha_ids:
        return {
            "alpha_ids": [],
            "matrix": [],
            "pearson_matrix": [],
            "spearman_matrix": [],
            "sample_length": 0,
        }

    if data.shape[1] < 2:
        eye = np.eye(len(alpha_ids), dtype=np.float64)
        matrix = eye.tolist()
        return {
            "alpha_ids": alpha_ids,
            "matrix": matrix,
            "pearson_matrix": matrix,
            "spearman_matrix": matrix,
            "sample_length": int(data.shape[1]),
        }

    pearson = np.nan_to_num(np.corrcoef(data), nan=0.0, posinf=0.0, neginf=0.0)
    ranked = np.apply_along_axis(_rank_1d, 1, data)
    spearman = np.nan_to_num(np.corrcoef(ranked), nan=0.0, posinf=0.0, neginf=0.0)
    return {
        "alpha_ids": alpha_ids,
        "matrix": pearson.tolist(),  # Backward-compatible alias.
        "pearson_matrix": pearson.tolist(),
        "spearman_matrix": spearman.tolist(),
        "sample_length": int(data.shape[1]),
    }


def flag_redundant_pairs(
    matrix_payload: dict[str, Any],
    *,
    threshold: float = 0.7,
    metric: str = "pearson",
) -> list[dict[str, Any]]:
    alpha_ids = list(matrix_payload.get("alpha_ids", []))
    matrix_key = "spearman_matrix" if metric == "spearman" else "pearson_matrix"
    raw_matrix = np.asarray(matrix_payload.get(matrix_key) or matrix_payload.get("matrix", []), dtype=np.float64)
    if raw_matrix.ndim != 2 or raw_matrix.shape[0] != raw_matrix.shape[1]:
        return []

    redundant: list[dict[str, Any]] = []
    for i in range(raw_matrix.shape[0]):
        for j in range(i + 1, raw_matrix.shape[1]):
            corr = float(raw_matrix[i, j])
            if abs(corr) >= float(threshold):
                redundant.append(
                    {
                        "alpha_a": alpha_ids[i],
                        "alpha_b": alpha_ids[j],
                        "correlation": corr,
                        "metric": metric,
                    }
                )
    redundant.sort(key=lambda item: abs(float(item["correlation"])), reverse=True)
    return redundant


def optimize_pool_weights(
    *,
    base_dir: str = "research/experiments",
    method: str = "equal_weight",
    ridge_alpha: float = 0.1,
    signals: Mapping[str, Any] | None = None,
    returns: Any | None = None,
) -> PoolOptimizationResult:
    tracker = ExperimentTracker(base_dir=base_dir)
    source_signals = signals if signals is not None else tracker.latest_signals_by_alpha()
    if not source_signals:
        return PoolOptimizationResult(
            method=method,
            alpha_ids=tuple(),
            weights={},
            returns_used=False,
            diagnostics={"reason": "no_signals"},
        )

    alpha_ids, data = _aligned_signal_matrix(source_signals, sample_step=1)
    if not alpha_ids:
        return PoolOptimizationResult(
            method=method,
            alpha_ids=tuple(),
            weights={},
            returns_used=False,
            diagnostics={"reason": "invalid_signals"},
        )

    returns_arr = _coerce_returns(returns)
    if returns_arr is None:
        returns_arr = tracker.proxy_returns()

    weights_arr, diagnostics = _optimize_weights_array(
        data=data,
        returns=returns_arr,
        method=method,
        ridge_alpha=float(ridge_alpha),
    )
    weights = {alpha_id: float(weight) for alpha_id, weight in zip(alpha_ids, weights_arr)}
    diagnostics["sample_length"] = int(data.shape[1])
    return PoolOptimizationResult(
        method=method,
        alpha_ids=tuple(alpha_ids),
        weights=weights,
        returns_used=returns_arr is not None,
        diagnostics=diagnostics,
    )


def marginal_contribution_test(
    *,
    new_signal: Any,
    existing_signals: Mapping[str, Any],
    method: str = "equal_weight",
    min_uplift: float = 0.05,
    ridge_alpha: float = 0.1,
    returns: Any | None = None,
) -> dict[str, Any]:
    if not existing_signals:
        return {
            "method": method,
            "returns_used": returns is not None,
            "baseline_metric": 0.0,
            "candidate_metric": 1.0,
            "uplift": 1.0,
            "min_uplift": float(min_uplift),
            "passed": True,
            "reason": "no_existing_pool",
        }

    existing_ids, existing_data = _aligned_signal_matrix(existing_signals, sample_step=1)
    if not existing_ids:
        return {
            "method": method,
            "returns_used": returns is not None,
            "baseline_metric": 0.0,
            "candidate_metric": 0.0,
            "uplift": 0.0,
            "min_uplift": float(min_uplift),
            "passed": False,
            "reason": "invalid_existing_pool",
        }

    candidate = np.asarray(new_signal, dtype=np.float64)
    min_len = min(existing_data.shape[1], candidate.size)
    if min_len < 2:
        return {
            "method": method,
            "returns_used": returns is not None,
            "baseline_metric": 0.0,
            "candidate_metric": 0.0,
            "uplift": 0.0,
            "min_uplift": float(min_uplift),
            "passed": False,
            "reason": "insufficient_samples",
        }

    base = existing_data[:, :min_len]
    candidate_row = candidate[:min_len].reshape(1, -1)
    stacked = np.vstack([base, candidate_row])
    returns_arr = _coerce_returns(returns)

    base_weights, _ = _optimize_weights_array(base, returns_arr, method=method, ridge_alpha=float(ridge_alpha))
    candidate_weights, _ = _optimize_weights_array(stacked, returns_arr, method=method, ridge_alpha=float(ridge_alpha))

    baseline_metric, metric_name = _pool_metric(base, base_weights, returns_arr)
    candidate_metric, _ = _pool_metric(stacked, candidate_weights, returns_arr)
    uplift = _relative_uplift(candidate_metric, baseline_metric)
    return {
        "method": method,
        "metric_name": metric_name,
        "returns_used": returns_arr is not None,
        "baseline_metric": float(baseline_metric),
        "candidate_metric": float(candidate_metric),
        "uplift": float(uplift),
        "min_uplift": float(min_uplift),
        "passed": bool(uplift >= float(min_uplift)),
        "existing_alpha_ids": existing_ids,
    }


def evaluate_marginal_alpha(
    *,
    alpha_id: str,
    base_dir: str = "research/experiments",
    method: str = "equal_weight",
    min_uplift: float = 0.05,
    ridge_alpha: float = 0.1,
) -> dict[str, Any]:
    tracker = ExperimentTracker(base_dir=base_dir)
    signals = tracker.latest_signals_by_alpha()
    if alpha_id not in signals:
        raise ValueError(f"Unknown alpha_id '{alpha_id}' in latest experiment runs")

    new_signal = signals[alpha_id]
    existing = {key: value for key, value in signals.items() if key != alpha_id}
    returns = tracker.proxy_returns()
    out = marginal_contribution_test(
        new_signal=new_signal,
        existing_signals=existing,
        method=method,
        min_uplift=min_uplift,
        ridge_alpha=ridge_alpha,
        returns=returns,
    )
    out["alpha_id"] = alpha_id
    return out


def _aligned_signal_matrix(
    signals: Mapping[str, Any],
    *,
    sample_step: int,
) -> tuple[list[str], np.ndarray]:
    alpha_ids = sorted(signals)
    if not alpha_ids:
        return [], np.empty((0, 0), dtype=np.float64)

    rows = [np.asarray(signals[alpha_id], dtype=np.float64) for alpha_id in alpha_ids]
    min_len = min((row.size for row in rows), default=0)
    if min_len == 0:
        return [], np.empty((0, 0), dtype=np.float64)
    data = np.vstack([row[:min_len] for row in rows])
    if sample_step > 1:
        data = data[:, ::sample_step]
    return alpha_ids, np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)


def _rank_1d(row: np.ndarray) -> np.ndarray:
    order = np.argsort(row, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(order.size, dtype=np.float64)
    return ranks


def _coerce_returns(values: Sequence[float] | None) -> np.ndarray | None:
    if values is None:
        return None
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    if arr.size < 2:
        return None
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def _optimize_weights_array(
    data: np.ndarray,
    returns: np.ndarray | None,
    *,
    method: str,
    ridge_alpha: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    n_alpha = data.shape[0]
    if n_alpha == 1:
        return np.array([1.0], dtype=np.float64), {"strategy": "single_alpha"}

    normalized = _zscore_rows(data)
    method_key = str(method).strip().lower()
    if method_key == "ic_weighted":
        weights = _ic_weighted(normalized, returns)
        strategy = "ic_weighted" if returns is not None else "equal_weight_fallback_no_returns"
    elif method_key == "mean_variance":
        weights = _mean_variance(normalized, returns)
        strategy = "mean_variance"
    elif method_key == "ridge":
        weights = _ridge(normalized, returns, ridge_alpha=ridge_alpha)
        strategy = "ridge"
    else:
        weights = _equal_weight(n_alpha)
        strategy = "equal_weight"

    return _normalize_weights(weights), {"strategy": strategy}


def _equal_weight(n_alpha: int) -> np.ndarray:
    return np.ones(n_alpha, dtype=np.float64) / max(1, n_alpha)


def _ic_weighted(data: np.ndarray, returns: np.ndarray | None) -> np.ndarray:
    if returns is None:
        return _equal_weight(data.shape[0])
    n = min(data.shape[1], returns.size)
    if n < 2:
        return _equal_weight(data.shape[0])

    target = returns[:n]
    ic = np.zeros(data.shape[0], dtype=np.float64)
    for i in range(data.shape[0]):
        corr = np.corrcoef(data[i, :n], target)[0, 1]
        ic[i] = float(corr) if np.isfinite(corr) else 0.0
    if np.allclose(ic, 0.0):
        return _equal_weight(data.shape[0])
    return ic


def _mean_variance(data: np.ndarray, returns: np.ndarray | None) -> np.ndarray:
    cov = np.cov(data)
    cov = np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0)
    cov += np.eye(cov.shape[0], dtype=np.float64) * 1e-6
    inv_cov = np.linalg.pinv(cov)
    if returns is None:
        mu = np.ones(data.shape[0], dtype=np.float64)
    else:
        n = min(data.shape[1], returns.size)
        if n < 2:
            mu = np.ones(data.shape[0], dtype=np.float64)
        else:
            mu = np.array(
                [np.corrcoef(data[i, :n], returns[:n])[0, 1] for i in range(data.shape[0])],
                dtype=np.float64,
            )
            mu = np.nan_to_num(mu, nan=0.0, posinf=0.0, neginf=0.0)
            if np.allclose(mu, 0.0):
                mu = np.ones(data.shape[0], dtype=np.float64)
    return inv_cov @ mu


def _ridge(data: np.ndarray, returns: np.ndarray | None, *, ridge_alpha: float) -> np.ndarray:
    y: np.ndarray
    if returns is None:
        y = np.nanmean(data, axis=0)
    else:
        n = min(data.shape[1], returns.size)
        y = returns[:n]
        data = data[:, :n]
    gram = data @ data.T
    reg = gram + (max(1e-8, float(ridge_alpha)) * np.eye(gram.shape[0], dtype=np.float64))
    rhs = data @ y
    return np.linalg.solve(reg, rhs)


def _normalize_weights(weights: np.ndarray) -> np.ndarray:
    out = np.asarray(weights, dtype=np.float64).reshape(-1)
    if out.size == 0:
        return out
    if np.allclose(out, 0.0):
        out = np.ones(out.size, dtype=np.float64)
    denom = float(np.sum(np.abs(out)))
    if denom <= 1e-12:
        return np.ones(out.size, dtype=np.float64) / out.size
    return out / denom


def _zscore_rows(data: np.ndarray) -> np.ndarray:
    mean = np.mean(data, axis=1, keepdims=True)
    std = np.std(data, axis=1, keepdims=True)
    return np.divide(data - mean, std, out=np.zeros_like(data), where=std > 1e-12)


def _pool_metric(
    data: np.ndarray,
    weights: np.ndarray,
    returns: np.ndarray | None,
) -> tuple[float, str]:
    n = data.shape[1]
    combined = np.sum(weights.reshape(-1, 1) * data, axis=0)
    if returns is None:
        if data.shape[0] < 2:
            return 0.0, "diversification_score"
        corr = np.nan_to_num(np.corrcoef(data), nan=0.0, posinf=0.0, neginf=0.0)
        weighted_corr = 0.0
        for i in range(corr.shape[0]):
            for j in range(i + 1, corr.shape[1]):
                weighted_corr += abs(float(corr[i, j])) * abs(float(weights[i] * weights[j]))
        return float(1.0 - weighted_corr), "diversification_score"

    m = min(n, returns.size)
    if m < 3:
        return 0.0, "pool_sharpe"
    pnl = combined[:m] * returns[:m]
    sigma = float(np.std(pnl))
    if sigma <= 1e-12:
        return 0.0, "pool_sharpe"
    sharpe = float(np.mean(pnl) / sigma * np.sqrt(252.0))
    return sharpe, "pool_sharpe"


def _relative_uplift(candidate: float, baseline: float) -> float:
    if abs(baseline) <= 1e-12:
        return 0.0 if abs(candidate) <= 1e-12 else float(np.sign(candidate))
    return float((candidate - baseline) / abs(baseline))

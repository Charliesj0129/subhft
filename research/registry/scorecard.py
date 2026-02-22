from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from research.registry.schemas import Scorecard


def compute_scorecard(
    result: Mapping[str, Any],
    pool_signals: Mapping[str, Sequence[float]] | None = None,
) -> Scorecard:
    sharpe_is = _as_float(result.get("sharpe_is"))
    sharpe_oos = _as_float(result.get("sharpe_oos"))
    ic_mean = _as_float(result.get("ic_mean"))
    ic_std = _as_float(result.get("ic_std"))
    turnover = _as_float(result.get("turnover"))
    max_drawdown = _as_float(result.get("max_drawdown"))
    regime = _to_regime_dict(result.get("regime_metrics"))
    capacity = _as_float(result.get("capacity_estimate"))

    corr_max = None
    signal = result.get("signals")
    if signal is not None and pool_signals:
        corr_max = _max_pool_correlation(np.asarray(signal, dtype=np.float64), pool_signals)

    return Scorecard(
        sharpe_is=sharpe_is,
        sharpe_oos=sharpe_oos,
        ic_mean=ic_mean,
        ic_std=ic_std,
        turnover=turnover,
        max_drawdown=max_drawdown,
        correlation_pool_max=corr_max,
        regime_sharpe=regime,
        capacity_estimate=capacity,
    )


def save_scorecard(path: str | Path, scorecard: Scorecard) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scorecard.to_dict(), indent=2, sort_keys=True))


def load_scorecard(path: str | Path) -> Scorecard:
    payload = json.loads(Path(path).read_text())
    return Scorecard.from_dict(payload)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_regime_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        casted = _as_float(raw)
        if casted is not None:
            out[str(key)] = casted
    return out


def _max_pool_correlation(signal: np.ndarray, pool_signals: Mapping[str, Sequence[float]]) -> float | None:
    if signal.size == 0:
        return None
    corr_values: list[float] = []
    for _, pool_signal in pool_signals.items():
        arr = np.asarray(pool_signal, dtype=np.float64)
        n = min(signal.size, arr.size)
        if n < 2:
            continue
        matrix = np.corrcoef(signal[:n], arr[:n])
        value = float(matrix[0, 1])
        if np.isfinite(value):
            corr_values.append(abs(value))
    if not corr_values:
        return None
    return max(corr_values)

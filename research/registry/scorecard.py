from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from research.registry.schemas import Scorecard


def compute_scorecard(
    result: Mapping[str, Any],
    pool_signals: Mapping[str, Sequence[float]] | None = None,
    wf_extra: Mapping[str, Any] | None = None,
    data_meta_path: str | None = None,
) -> Scorecard:
    sharpe_is = _as_float(result.get("sharpe_is"))
    sharpe_oos = _as_float(result.get("sharpe_oos"))
    ic_mean = _as_float(result.get("ic_mean"))
    ic_std = _as_float(result.get("ic_std"))
    turnover = _as_float(result.get("turnover"))
    max_drawdown = _as_float(result.get("max_drawdown"))
    regime = _to_regime_dict(result.get("regime_metrics"))
    capacity = _as_float(result.get("capacity_estimate"))
    raw_latency = result.get("latency_profile")
    latency_profile = (
        {str(k): v for k, v in dict(raw_latency).items()}
        if isinstance(raw_latency, Mapping)
        else None
    )

    corr_max = 0.0
    signal = result.get("signals")
    if signal is not None and pool_signals:
        computed_corr = _max_pool_correlation(np.asarray(signal, dtype=np.float64), pool_signals)
        if computed_corr is not None:
            corr_max = float(computed_corr)
    wf = wf_extra or {}

    # Stage 6 cost sensitivity: prefer explicit field; else compute from
    # avg_spread_cost and ic_mean as a proxy when both are available.
    cost_sensitivity_ratio = _as_float(result.get("cost_sensitivity_ratio"))
    if cost_sensitivity_ratio is None:
        avg_spread_cost = _as_float(result.get("avg_spread_cost"))
        if avg_spread_cost is not None and avg_spread_cost > 0 and ic_mean is not None:
            cost_sensitivity_ratio = abs(ic_mean) / avg_spread_cost

    meta_payload = _load_data_meta(data_meta_path)
    rng_seed = _as_int(meta_payload.get("rng_seed")) if meta_payload else None
    generator_script = (
        str(meta_payload.get("generator_script"))
        if meta_payload and meta_payload.get("generator_script") is not None
        else None
    )
    data_ul = _as_int(meta_payload.get("data_ul")) if meta_payload else None
    data_fingerprint = _fingerprint_from_meta(data_meta_path=data_meta_path, meta_payload=meta_payload)
    regime_ic = _to_regime_dict(result.get("regime_ic"))

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
        latency_profile=latency_profile,
        walk_forward_sharpe_mean=_as_float(wf.get("walk_forward_sharpe_mean")),
        walk_forward_sharpe_std=_as_float(wf.get("walk_forward_sharpe_std")),
        walk_forward_sharpe_min=_as_float(wf.get("walk_forward_sharpe_min")),
        walk_forward_consistency_pct=_as_float(wf.get("walk_forward_consistency_pct")),
        stat_bh_n_survived=(
            int(wf["stat_bh_n_survived"]) if wf.get("stat_bh_n_survived") is not None else None
        ),
        stat_bh_method=str(wf["stat_bh_method"]) if wf.get("stat_bh_method") else None,
        stat_bds_pvalue=_as_float(wf.get("stat_bds_pvalue")),
        cost_sensitivity_ratio=cost_sensitivity_ratio,
        data_fingerprint=data_fingerprint,
        rng_seed=rng_seed,
        generator_script=generator_script,
        data_ul=data_ul,
        regime_ic=regime_ic,
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


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_data_meta(data_meta_path: str | None) -> dict[str, Any] | None:
    if not data_meta_path:
        return None
    path = Path(data_meta_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _fingerprint_from_meta(data_meta_path: str | None, meta_payload: Mapping[str, Any] | None) -> str | None:
    data_path: Path | None = None
    if meta_payload:
        candidate = meta_payload.get("data_file")
        if isinstance(candidate, str) and candidate.strip():
            cpath = Path(candidate.strip())
            if cpath.exists():
                data_path = cpath
    if data_path is None and data_meta_path:
        mpath = Path(data_meta_path)
        name = mpath.name
        if name.endswith(".meta.json"):
            data_path = mpath.with_name(name[: -len(".meta.json")])
        elif name.endswith(".metadata.json"):
            data_path = mpath.with_name(name[: -len(".metadata.json")])
    if data_path is None or not data_path.exists():
        return None
    try:
        with data_path.open("rb") as f:
            head = f.read(1024)
    except OSError:
        return None
    return hashlib.sha256(head).hexdigest()


def _to_regime_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        casted = _as_float(raw)
        if casted is not None:
            out[str(key)] = casted
    return out


def _pearson_pair(a: np.ndarray, b: np.ndarray) -> float:
    """Vectorised single-pair Pearson r.  Returns 0.0 when undefined."""
    a_m = a - a.mean()
    b_m = b - b.mean()
    num = float(np.dot(a_m, b_m))
    denom = float(np.sqrt(np.dot(a_m, a_m) * np.dot(b_m, b_m)))
    if denom < 1e-12:
        return 0.0
    val = num / denom
    return val if np.isfinite(val) else 0.0


def _max_pool_correlation(
    signal: np.ndarray,
    pool_signals: Mapping[str, Sequence[float]],
    prev_matrix: np.ndarray | None = None,
) -> float | None:
    """Compute the maximum absolute correlation between *signal* and the pool.

    Parameters
    ----------
    signal:
        The new alpha signal vector.
    pool_signals:
        Existing pool signals keyed by alpha id.
    prev_matrix:
        Optional correlation matrix from a prior pool computation.
        Currently accepted for API compatibility but not used; callers
        wanting full incremental matrix updates should use
        :func:`compute_pool_correlation_matrix` instead.

    Returns
    -------
    float | None
        Maximum absolute pairwise correlation, or ``None`` when no
        valid correlation could be computed.
    """
    if signal.size == 0:
        return None
    corr_values: list[float] = []
    for _, pool_signal in pool_signals.items():
        arr = np.asarray(pool_signal, dtype=np.float64)
        n = min(signal.size, arr.size)
        if n < 2:
            continue
        value = _pearson_pair(signal[:n], arr[:n])
        if value != 0.0:
            corr_values.append(abs(value))
    if not corr_values:
        return None
    return max(corr_values)


def compute_pool_correlation_matrix(
    pool_signals: Mapping[str, Sequence[float]],
    prev_matrix: np.ndarray | None = None,
    prev_keys: Sequence[str] | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Build or incrementally update a pool correlation matrix.

    Parameters
    ----------
    pool_signals:
        All pool signals keyed by alpha id (including any new alpha).
    prev_matrix:
        The correlation matrix from a previous call (shape
        ``(K, K)``).  When provided together with *prev_keys*, only
        the correlations for newly added alphas are computed.
    prev_keys:
        Ordered alpha id list that corresponds to *prev_matrix* rows
        and columns.

    Returns
    -------
    (matrix, keys)
        ``matrix`` has shape ``(N, N)`` with ``N = len(pool_signals)``.
        ``keys`` is the ordered list of alpha ids matching the matrix
        axes.
    """
    keys = list(pool_signals.keys())
    n = len(keys)
    if n == 0:
        return np.empty((0, 0), dtype=np.float64), []

    # Determine which keys are new.
    prev_key_set: set[str] = set()
    prev_key_list: list[str] = []
    if prev_matrix is not None and prev_keys is not None:
        prev_key_list = list(prev_keys)
        prev_key_set = set(prev_key_list)

    # If we can reuse the previous matrix, do an incremental update.
    if (
        prev_matrix is not None
        and prev_key_list
        and prev_matrix.shape[0] == len(prev_key_list)
        and prev_key_set.issubset(set(keys))
    ):
        new_keys = [k for k in keys if k not in prev_key_set]
        if not new_keys:
            # No new alphas — reorder prev_matrix to match current keys
            idx = [prev_key_list.index(k) for k in keys]
            reordered = prev_matrix[np.ix_(idx, idx)]
            return reordered, keys

        # Build full matrix: copy prior block, compute new rows/columns
        # Reorder keys so prior ones come first, new ones at end
        ordered_keys = [k for k in keys if k in prev_key_set] + new_keys
        m_prev = len(prev_key_list)
        m_total = len(ordered_keys)
        matrix = np.eye(m_total, dtype=np.float64)

        # Copy prior sub-block (reordered to match ordered_keys)
        prior_idx = [prev_key_list.index(k) for k in ordered_keys[:m_prev]]
        matrix[:m_prev, :m_prev] = prev_matrix[np.ix_(prior_idx, prior_idx)]

        # Pre-compute arrays for all signals
        arrs: dict[str, np.ndarray] = {}
        for k in ordered_keys:
            arrs[k] = np.asarray(pool_signals[k], dtype=np.float64)

        # Compute correlations for each new alpha against all others
        for i, nk in enumerate(new_keys):
            row_idx = m_prev + i
            a = arrs[nk]
            for j, ok in enumerate(ordered_keys):
                if j == row_idx:
                    continue
                b = arrs[ok]
                nn = min(a.size, b.size)
                if nn < 2:
                    matrix[row_idx, j] = 0.0
                    matrix[j, row_idx] = 0.0
                    continue
                val = _pearson_pair(a[:nn], b[:nn])
                matrix[row_idx, j] = val
                matrix[j, row_idx] = val

        # Reorder to match the original keys order
        final_idx = [ordered_keys.index(k) for k in keys]
        return matrix[np.ix_(final_idx, final_idx)], keys

    # Full computation — no prior matrix or incompatible
    arrs_list: list[np.ndarray] = []
    min_len = None
    for k in keys:
        a = np.asarray(pool_signals[k], dtype=np.float64)
        arrs_list.append(a)
        if min_len is None or a.size < min_len:
            min_len = a.size

    if min_len is None or min_len < 2:
        return np.eye(n, dtype=np.float64), keys

    if n == 1:
        return np.ones((1, 1), dtype=np.float64), keys

    stacked = np.column_stack([a[:min_len] for a in arrs_list])  # (min_len, n)
    matrix = np.corrcoef(stacked, rowvar=False)  # (n, n)
    # Replace NaN with 0 for safety
    matrix = np.where(np.isfinite(matrix), matrix, 0.0)
    return matrix, keys

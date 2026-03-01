from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TemporalSplit:
    is_idx: np.ndarray
    oos_idx: np.ndarray


def split_is_oos(length: int, is_ratio: float = 0.7) -> TemporalSplit:
    if length <= 0:
        return TemporalSplit(is_idx=np.asarray([], dtype=np.int64), oos_idx=np.asarray([], dtype=np.int64))
    cut = int(length * is_ratio)
    cut = min(max(cut, 1), max(length - 1, 1))
    idx = np.arange(length, dtype=np.int64)
    return TemporalSplit(is_idx=idx[:cut], oos_idx=idx[cut:])


def split_by_vol_regime(forward_returns: np.ndarray) -> dict[str, np.ndarray]:
    ret = np.asarray(forward_returns, dtype=np.float64)
    if ret.size == 0:
        return {"high_vol": np.asarray([], dtype=np.int64), "low_vol": np.asarray([], dtype=np.int64)}
    vol = np.abs(ret)
    median = float(np.median(vol))
    idx = np.arange(ret.size, dtype=np.int64)
    return {"high_vol": idx[vol >= median], "low_vol": idx[vol < median]}


def cusum_breakpoints(
    series: np.ndarray,
    threshold: float = 5.0,
) -> list[int]:
    """Detect structural breakpoints using CUSUM on standardized deviations.

    Returns indices where the CUSUM statistic exceeds *threshold* standard
    deviations and resets.  High-count → regime instability → alpha risk.

    Args:
        series: 1-d signal or returns array.
        threshold: Number of std-devs to trigger a breakpoint reset.
    """
    arr = np.asarray(series, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 8:
        return []

    mean = float(arr.mean())
    std = float(arr.std())
    if std < 1e-15:
        return []

    breaks: list[int] = []
    cusum_pos = 0.0
    cusum_neg = 0.0
    for i in range(n):
        z = (float(arr[i]) - mean) / std
        cusum_pos = max(0.0, cusum_pos + z)
        cusum_neg = min(0.0, cusum_neg + z)
        if cusum_pos > threshold or cusum_neg < -threshold:
            breaks.append(i)
            cusum_pos = 0.0
            cusum_neg = 0.0
    return breaks


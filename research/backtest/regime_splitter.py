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

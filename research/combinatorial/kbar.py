"""K-bar (OHLCV) shape features for governed alpha mining.

Every feature is a self-normalized ratio or return built from the per-bar
OHLCV columns already carried by ``BarDataset``
(``research/combinatorial/smma_dataset.py``) — no new dataset or ClickHouse
query is required, exactly like ``research/combinatorial/bidask.py``. Where
the smma family asks "where is price relative to its moving averages", this
family asks "what shape is the bar itself": body/wick proportions, intrabar
return, normalized range, close location, overnight/session gap, and volume
change. The GP layer never sees a raw price level or a raw volume count, only
stationary derived quantities, mirroring the discipline in
``research/combinatorial/smma.py``.

Cross-bar features (gap, volume change) are computed per reset segment so a
contract roll, session boundary, or data gap never leaks across the seam.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

KBAR_DELTA_LAGS: tuple[int, ...] = (1, 3, 6)

_KBAR_BASE_FEATURE_HISTORY_BARS: dict[str, int] = {
    "kbar_open_close_return": 1,
    "kbar_body_ratio": 1,
    "kbar_upper_wick_ratio": 1,
    "kbar_lower_wick_ratio": 1,
    "kbar_range_ratio": 1,
    "kbar_close_location_ratio": 1,
    "kbar_gap_return": 2,
    "kbar_volume_change_ratio": 2,
}

# Exact raw-bar history for every exported kbar feature.  A delta at lag L
# needs both the base feature now and at t-L, so its history is base+L.
KBAR_FEATURE_HISTORY_BARS: dict[str, int] = {
    **_KBAR_BASE_FEATURE_HISTORY_BARS,
    **{
        f"{name}_delta{lag}": base_history + lag
        for name, base_history in _KBAR_BASE_FEATURE_HISTORY_BARS.items()
        for lag in KBAR_DELTA_LAGS
    },
}


def _safe_normalize(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.full(numerator.size, np.nan, dtype=np.float64)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (np.abs(denominator) > 1e-12)
    np.divide(numerator, denominator, out=out, where=valid)
    return out


def _segmented_lag_delta(values: np.ndarray, lag: int, reset_mask: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.full(values.size, np.nan, dtype=np.float64)
    segment_start = 0
    for index in range(values.size):
        if reset_mask[index]:
            segment_start = index
        previous_index = index - lag
        if previous_index < segment_start:
            continue
        if np.isfinite(values[index]) and np.isfinite(values[previous_index]):
            out[index] = values[index] - values[previous_index]
    return out


def _segmented_previous(values: np.ndarray, reset_mask: np.ndarray) -> np.ndarray:
    """Previous in-segment value; NaN at every segment start (no cross-seam leak)."""
    out: np.ndarray = np.full(values.size, np.nan, dtype=np.float64)
    segment_start = 0
    for index in range(values.size):
        if reset_mask[index]:
            segment_start = index
        if index - 1 < segment_start:
            continue
        out[index] = values[index - 1]
    return out


def build_kbar_family_features(
    *,
    open_: Sequence[float] | np.ndarray,
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    volume: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    lags: Sequence[int] = KBAR_DELTA_LAGS,
) -> dict[str, np.ndarray]:
    """Compute the fixed kbar family, exposing stationary features only."""
    arrays = {
        "open": np.asarray(open_, dtype=np.float64).reshape(-1),
        "high": np.asarray(high, dtype=np.float64).reshape(-1),
        "low": np.asarray(low, dtype=np.float64).reshape(-1),
        "close": np.asarray(close, dtype=np.float64).reshape(-1),
        "volume": np.asarray(volume, dtype=np.float64).reshape(-1),
    }
    if len({array.size for array in arrays.values()}) != 1:
        raise ValueError("kbar input arrays must have identical lengths")
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if resets.size != arrays["open"].size:
        raise ValueError("reset_mask length must match kbar input arrays")
    lag_values = tuple(int(lag) for lag in lags)
    if not lag_values or any(lag < 1 for lag in lag_values):
        raise ValueError("lags must be a non-empty sequence of positive integers")

    bar_range = arrays["high"] - arrays["low"]
    body = arrays["close"] - arrays["open"]
    body_top = np.maximum(arrays["open"], arrays["close"])
    body_bottom = np.minimum(arrays["open"], arrays["close"])
    previous_close = _segmented_previous(arrays["close"], resets)
    previous_volume = _segmented_previous(arrays["volume"], resets)

    base_features: dict[str, np.ndarray] = {
        # Intrabar directional return, scale-free across roots and contracts.
        "kbar_open_close_return": _safe_normalize(body, arrays["open"]),
        # Body as a share of the bar's full range: conviction vs indecision.
        "kbar_body_ratio": _safe_normalize(body, bar_range),
        # Rejection wicks, each as a share of the full range.
        "kbar_upper_wick_ratio": _safe_normalize(arrays["high"] - body_top, bar_range),
        "kbar_lower_wick_ratio": _safe_normalize(body_bottom - arrays["low"], bar_range),
        # Normalized bar range: a per-bar realized-volatility proxy.
        "kbar_range_ratio": _safe_normalize(bar_range, arrays["close"]),
        # Zero-centred close location inside the range: -1 at the low, +1 at the high.
        "kbar_close_location_ratio": _safe_normalize(2.0 * arrays["close"] - arrays["high"] - arrays["low"], bar_range),
        # Gap from the previous in-segment bar's close to this bar's open.
        "kbar_gap_return": _safe_normalize(arrays["open"] - previous_close, previous_close),
        # Bounded volume change: -1 (collapse) .. +1 (burst), immune to root scale.
        "kbar_volume_change_ratio": _safe_normalize(
            arrays["volume"] - previous_volume, arrays["volume"] + previous_volume
        ),
    }

    features: dict[str, np.ndarray] = dict(base_features)
    for name, values in base_features.items():
        for lag in lag_values:
            features[f"{name}_delta{lag}"] = _segmented_lag_delta(values, lag, resets)
    return features

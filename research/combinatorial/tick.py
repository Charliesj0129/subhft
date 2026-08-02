"""Tick-aggregate features for governed alpha mining.

Every feature is a self-normalized ratio or count-imbalance of the per-bar
tick/quote aggregates carried by ``TickBarDataset``
(``research/combinatorial/tick_dataset.py``, Phase 1: trade/quote counts and
aggressor buy/sell split). The GP layer never sees a raw count, only
stationary derived quantities, mirroring the discipline in
``research/combinatorial/smma.py`` and ``research/combinatorial/bidask.py``.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

TICK_SLOPE_LAGS: tuple[int, ...] = (1, 3, 6)

_TICK_BASE_FEATURES: tuple[str, ...] = (
    "tick_buy_sell_imbalance",
    "tick_buy_sell_volume_ratio",
    "tick_intensity_ratio",
)

# Exact raw-bar history for every feature exported by
# ``build_tick_family_features``.
TICK_FEATURE_HISTORY_BARS: dict[str, int] = {
    **dict.fromkeys(_TICK_BASE_FEATURES, 1),
    **{f"{name}_delta{lag}": lag + 1 for name in _TICK_BASE_FEATURES for lag in TICK_SLOPE_LAGS},
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


def build_tick_family_features(
    *,
    trade_tick_count: Sequence[float] | np.ndarray,
    quote_update_count: Sequence[float] | np.ndarray,
    buy_tick_count: Sequence[float] | np.ndarray,
    sell_tick_count: Sequence[float] | np.ndarray,
    buy_tick_volume: Sequence[float] | np.ndarray,
    sell_tick_volume: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    lags: Sequence[int] = TICK_SLOPE_LAGS,
) -> dict[str, np.ndarray]:
    """Compute the fixed tick family, exposing stationary features only."""
    arrays = {
        "trade_tick_count": np.asarray(trade_tick_count, dtype=np.float64).reshape(-1),
        "quote_update_count": np.asarray(quote_update_count, dtype=np.float64).reshape(-1),
        "buy_tick_count": np.asarray(buy_tick_count, dtype=np.float64).reshape(-1),
        "sell_tick_count": np.asarray(sell_tick_count, dtype=np.float64).reshape(-1),
        "buy_tick_volume": np.asarray(buy_tick_volume, dtype=np.float64).reshape(-1),
        "sell_tick_volume": np.asarray(sell_tick_volume, dtype=np.float64).reshape(-1),
    }
    if len({array.size for array in arrays.values()}) != 1:
        raise ValueError("tick input arrays must have identical lengths")
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if resets.size != arrays["trade_tick_count"].size:
        raise ValueError("reset_mask length must match tick input arrays")
    lag_values = tuple(int(lag) for lag in lags)
    if not lag_values or any(lag < 1 for lag in lag_values):
        raise ValueError("lags must be a non-empty sequence of positive integers")

    base_features: dict[str, np.ndarray] = {
        "tick_buy_sell_imbalance": _safe_normalize(
            arrays["buy_tick_count"] - arrays["sell_tick_count"], arrays["trade_tick_count"]
        ),
        "tick_buy_sell_volume_ratio": _safe_normalize(
            arrays["buy_tick_volume"] - arrays["sell_tick_volume"],
            arrays["buy_tick_volume"] + arrays["sell_tick_volume"],
        ),
        "tick_intensity_ratio": _safe_normalize(arrays["trade_tick_count"], arrays["quote_update_count"]),
    }

    features: dict[str, np.ndarray] = dict(base_features)
    for name, values in base_features.items():
        for lag in lag_values:
            features[f"{name}_delta{lag}"] = _segmented_lag_delta(values, lag, resets)
    return features

"""Bid/ask microstructure features for governed alpha mining.

Every feature is a self-normalized ratio, spread, or delta of the per-bar
top-of-book quote already carried by ``BarDataset``
(``research/combinatorial/smma_dataset.py``) — no new dataset or ClickHouse
query is required. The GP layer never sees a raw bid/ask level, only
stationary derived quantities (spread fraction, quote imbalance, quote-size
ratio), mirroring the discipline in ``research/combinatorial/smma.py``.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

BIDASK_SLOPE_LAGS: tuple[int, ...] = (1, 3, 6)

_BIDASK_BASE_FEATURES: tuple[str, ...] = (
    "bidask_spread_frac_open",
    "bidask_spread_frac_close",
    "bidask_qty_imbalance_open",
    "bidask_qty_imbalance_close",
    "bidask_qty_ratio_open",
    "bidask_qty_ratio_close",
    "bidask_mid_shift_ratio",
)

# Exact raw-bar history for every feature exported by
# ``build_bidask_family_features``.  Keeping this metadata beside the feature
# definitions avoids reverse-engineering lookback from names in validation.
BIDASK_FEATURE_HISTORY_BARS: dict[str, int] = {
    **dict.fromkeys(_BIDASK_BASE_FEATURES, 1),
    "bidask_spread_frac_delta_open_close": 1,
    "bidask_qty_imbalance_delta_open_close": 1,
    **{f"{name}_slope{lag}": lag + 1 for name in _BIDASK_BASE_FEATURES for lag in BIDASK_SLOPE_LAGS},
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


def build_bidask_family_features(
    *,
    bid_open: Sequence[float] | np.ndarray,
    ask_open: Sequence[float] | np.ndarray,
    bid_qty_open: Sequence[float] | np.ndarray,
    ask_qty_open: Sequence[float] | np.ndarray,
    bid_close: Sequence[float] | np.ndarray,
    ask_close: Sequence[float] | np.ndarray,
    bid_qty_close: Sequence[float] | np.ndarray,
    ask_qty_close: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    lags: Sequence[int] = BIDASK_SLOPE_LAGS,
) -> dict[str, np.ndarray]:
    """Compute the fixed bidask family, exposing stationary features only."""
    arrays = {
        "bid_open": np.asarray(bid_open, dtype=np.float64).reshape(-1),
        "ask_open": np.asarray(ask_open, dtype=np.float64).reshape(-1),
        "bid_qty_open": np.asarray(bid_qty_open, dtype=np.float64).reshape(-1),
        "ask_qty_open": np.asarray(ask_qty_open, dtype=np.float64).reshape(-1),
        "bid_close": np.asarray(bid_close, dtype=np.float64).reshape(-1),
        "ask_close": np.asarray(ask_close, dtype=np.float64).reshape(-1),
        "bid_qty_close": np.asarray(bid_qty_close, dtype=np.float64).reshape(-1),
        "ask_qty_close": np.asarray(ask_qty_close, dtype=np.float64).reshape(-1),
    }
    if len({array.size for array in arrays.values()}) != 1:
        raise ValueError("bidask input arrays must have identical lengths")
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if resets.size != arrays["bid_open"].size:
        raise ValueError("reset_mask length must match bidask input arrays")
    lag_values = tuple(int(lag) for lag in lags)
    if not lag_values or any(lag < 1 for lag in lag_values):
        raise ValueError("lags must be a non-empty sequence of positive integers")

    mid_open = (arrays["bid_open"] + arrays["ask_open"]) / 2.0
    mid_close = (arrays["bid_close"] + arrays["ask_close"]) / 2.0
    spread_open = arrays["ask_open"] - arrays["bid_open"]
    spread_close = arrays["ask_close"] - arrays["bid_close"]

    base_features: dict[str, np.ndarray] = {
        "bidask_spread_frac_open": _safe_normalize(spread_open, mid_open),
        "bidask_spread_frac_close": _safe_normalize(spread_close, mid_close),
        "bidask_qty_imbalance_open": _safe_normalize(
            arrays["bid_qty_open"] - arrays["ask_qty_open"],
            arrays["bid_qty_open"] + arrays["ask_qty_open"],
        ),
        "bidask_qty_imbalance_close": _safe_normalize(
            arrays["bid_qty_close"] - arrays["ask_qty_close"],
            arrays["bid_qty_close"] + arrays["ask_qty_close"],
        ),
        "bidask_qty_ratio_open": _safe_normalize(arrays["bid_qty_open"], arrays["ask_qty_open"]),
        "bidask_qty_ratio_close": _safe_normalize(arrays["bid_qty_close"], arrays["ask_qty_close"]),
        "bidask_mid_shift_ratio": _safe_normalize(mid_close - mid_open, spread_open),
    }

    features: dict[str, np.ndarray] = dict(base_features)
    features["bidask_spread_frac_delta_open_close"] = (
        base_features["bidask_spread_frac_close"] - base_features["bidask_spread_frac_open"]
    )
    features["bidask_qty_imbalance_delta_open_close"] = (
        base_features["bidask_qty_imbalance_close"] - base_features["bidask_qty_imbalance_open"]
    )
    for name, values in base_features.items():
        for lag in lag_values:
            features[f"{name}_slope{lag}"] = _segmented_lag_delta(values, lag, resets)
    return features

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np


@dataclass(frozen=True)
class EquitySeries:
    timestamps_ns: np.ndarray
    equity: np.ndarray

    def is_valid(self) -> bool:
        return self.timestamps_ns.size >= 2 and self.equity.size >= 2


def mark_to_market_equity(
    cash: Iterable[float],
    positions: Iterable[float],
    mid_prices: Iterable[float],
) -> np.ndarray:
    cash_arr = np.asarray(list(cash), dtype=np.float64)
    pos_arr = np.asarray(list(positions), dtype=np.float64)
    mid_arr = np.asarray(list(mid_prices), dtype=np.float64)
    if not (cash_arr.shape == pos_arr.shape == mid_arr.shape):
        raise ValueError("cash, positions, and mid_prices must share the same shape")
    return cash_arr + (pos_arr * mid_arr)


def extract_equity_series(source: Any, asset_id: int = 0) -> EquitySeries | None:
    """Best-effort extraction for equity samples from adapter or hftbacktest objects."""
    from_trace = _extract_from_trace_like(source)
    if from_trace is not None:
        return from_trace

    hbt = getattr(source, "hbt", None)
    for candidate in (source, hbt):
        if candidate is None:
            continue
        from_stats = _extract_from_stats(candidate, asset_id)
        if from_stats is not None:
            return from_stats
        from_fields = _extract_from_named_fields(candidate)
        if from_fields is not None:
            return from_fields
    return None


def _extract_from_trace_like(source: Any) -> EquitySeries | None:
    ts = getattr(source, "equity_timestamps_ns", None)
    eq = getattr(source, "equity_values", None)
    if ts is None or eq is None:
        return None
    return _build_series(ts, eq)


def _extract_from_stats(hbt: Any, asset_id: int) -> EquitySeries | None:
    stats_fn = getattr(hbt, "stats", None)
    if not callable(stats_fn):
        return None

    for args in ((), (asset_id,)):
        try:
            stats_obj = stats_fn(*args)
        except TypeError:
            continue
        except Exception:
            return None
        from_stats = _extract_from_named_fields(stats_obj)
        if from_stats is not None:
            return from_stats
    return None


def _extract_from_named_fields(container: Any) -> EquitySeries | None:
    candidate_pairs = [
        ("timestamps", "equity"),
        ("timestamp", "equity"),
        ("time", "equity"),
        ("ts", "equity"),
        ("equity_t", "equity_v"),
        ("equity_time", "equity"),
        ("timestamp", "equity_curve"),
    ]
    for ts_name, eq_name in candidate_pairs:
        ts = _read_value(container, ts_name)
        eq = _read_value(container, eq_name)
        if ts is None or eq is None:
            continue
        series = _build_series(ts, eq)
        if series is not None:
            return series
    return None


def _read_value(container: Any, key: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(key)
    return getattr(container, key, None)


def _build_series(ts_raw: Any, eq_raw: Any) -> EquitySeries | None:
    ts = _coerce_1d_array(ts_raw, dtype=np.int64)
    eq = _coerce_1d_array(eq_raw, dtype=np.float64)
    if ts is None or eq is None:
        return None

    length = min(ts.size, eq.size)
    if length < 2:
        return None

    ts = ts[:length]
    eq = eq[:length]
    mask = np.isfinite(ts.astype(np.float64)) & np.isfinite(eq)
    ts = ts[mask]
    eq = eq[mask]
    if ts.size < 2 or eq.size < 2:
        return None
    return EquitySeries(timestamps_ns=ts.astype(np.int64, copy=False), equity=eq.astype(np.float64, copy=False))


def _coerce_1d_array(value: Any, dtype: Any) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        return None
    try:
        arr = np.asarray(value)
    except Exception:
        return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    try:
        return arr.astype(dtype, copy=False)
    except Exception:
        return None

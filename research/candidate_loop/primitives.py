"""Vectorized prim_v1 primitives and transforms over a per-day Panel (spec §7).

All windows are trailing-inclusive and point-in-time safe:

* delta-style windows (``depth_delta``, ``trade_imbalance``) anchor at the
  last row at or before ``t - window`` (event window: row ``i - N``), so the
  delta covers ``(t - window, t]``; warmup rows whose anchor falls before the
  first row are NaN;
* rolling-moment windows (``rolling_zscore``) cover the last ``N`` rows
  (event) or the rows in ``(t - window, t]`` (time), NaN-aware via cumulative
  sums with a minimum valid count; population std; NaN while the window is
  incomplete or the window std is 0;
* ``ema`` uses ``alpha = 2/(N+1)`` for event windows and per-step decay
  ``exp(-dt/window_ns)`` for time windows; NaN inputs carry the previous EMA
  forward (NaN until the first valid input).

``future_mid_return`` is LABEL ONLY: ``j = asof(local_ts[i] + h)`` and the
label is NaN once ``local_ts[i] + h`` passes the last row (no cross-day
labels).  Division is safe everywhere the spec says so: ``book_imbalance`` and
``trade_imbalance`` return 0 when their denominator is 0.

Argument-domain enforcement lives in ``validator.py``; these functions trust
canonical arguments (offline research path, float math allowed).
"""

from __future__ import annotations

import math
from typing import cast

import numpy as np

from research.candidate_loop.schema import Window, parse_window_spec

# Below this many finite values in a rolling window the moments are noise.
ZSCORE_MIN_VALID = 2

Columns = dict[str, np.ndarray]


def parse_canonical_window(spec: str) -> Window:
    """Parse validator-canonical specs (``'N_events'`` / ``'<ns>ns'``).

    Also accepts the raw ``'Nms'/'Ns'`` forms so candidate-level horizons can
    be passed through unchanged.
    """
    if spec.endswith("_events"):
        return parse_window_spec(spec)
    if spec.endswith("ns"):
        ns = int(spec[:-2])
        if ns <= 0:
            raise ValueError(f"Window/horizon must be positive: {spec!r}")
        return Window(kind="time", duration_ns=ns)
    return parse_window_spec(spec)


def safe_divide(num: np.ndarray | float, denom: np.ndarray | float) -> np.ndarray:
    """``num / denom`` with 0 where ``denom == 0`` (spec §7); NaN passes through."""
    num_arr = np.asarray(num, dtype=np.float64)
    denom_arr = np.asarray(denom, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom_arr == 0.0, 0.0, num_arr / denom_arr)


def trailing_anchor_indices(local_ts: np.ndarray, window: Window) -> np.ndarray:
    """Per-row anchor index for delta-style windows; negative means warmup.

    Event window N: anchor ``i - N``.  Time window d: last row ``j`` with
    ``local_ts[j] <= local_ts[i] - d`` (so deltas cover ``(t - d, t]``).
    """
    n = local_ts.size
    if window.kind == "events":
        return np.arange(n, dtype=np.int64) - window.count
    cutoff = local_ts - window.duration_ns
    return np.searchsorted(local_ts, cutoff, side="right").astype(np.int64) - 1


def _delta_from_anchor(values: np.ndarray, anchors: np.ndarray) -> np.ndarray:
    out = np.full(values.size, np.nan)
    valid = anchors >= 0
    out[valid] = values[valid] - values[anchors[valid]]
    return out


# ---------------------------------------------------------------------------
# Primitives.
# ---------------------------------------------------------------------------


def mid_price(cols: Columns) -> np.ndarray:
    return cols["mid"]


def spread_ticks(cols: Columns) -> np.ndarray:
    return cols["spread_ticks"]


def microprice(cols: Columns) -> np.ndarray:
    return cols["microprice"]


def depth_sum(cols: Columns, side: str, levels: int) -> np.ndarray:
    total = np.zeros_like(cols[f"{side}_qty_1"], dtype=np.float64)
    for lvl in range(1, levels + 1):
        total = total + cols[f"{side}_qty_{lvl}"]
    return total


def book_imbalance(cols: Columns, levels: int) -> np.ndarray:
    bid = depth_sum(cols, "bid", levels)
    ask = depth_sum(cols, "ask", levels)
    return safe_divide(bid - ask, bid + ask)


def depth_delta(cols: Columns, side: str, levels: int, window: Window) -> np.ndarray:
    anchors = trailing_anchor_indices(cols["local_ts"], window)
    return _delta_from_anchor(depth_sum(cols, side, levels), anchors)


def trade_imbalance(cols: Columns, window: Window) -> np.ndarray:
    """(Δbuy−Δsell)/(Δbuy+Δsell); 0 when no trades in window, NaN in warmup.

    Valid only on dir_clean days — the evaluator enforces that via panel meta.
    """
    anchors = trailing_anchor_indices(cols["local_ts"], window)
    d_buy = _delta_from_anchor(cols["trade_buy_qty"], anchors)
    d_sell = _delta_from_anchor(cols["trade_sell_qty"], anchors)
    return safe_divide(d_buy - d_sell, d_buy + d_sell)


def future_mid_return(cols: Columns, horizon: Window) -> np.ndarray:
    """LABEL ONLY: ``mid[asof(t + h)] / mid[t] − 1``; NaN past end-of-day."""
    local_ts = cols["local_ts"]
    mid = cols["mid"]
    n = mid.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    j: np.ndarray
    with np.errstate(invalid="ignore", divide="ignore"):
        if horizon.kind == "events":
            j = np.arange(n, dtype=np.int64) + horizon.count
            valid = j < n
            out[valid] = mid[j[valid]] / mid[valid] - 1.0
        else:
            target = local_ts + horizon.duration_ns
            j = np.searchsorted(local_ts, target, side="right").astype(np.int64) - 1
            valid = target <= local_ts[-1]
            out[valid] = mid[j[valid]] / mid[valid] - 1.0
    return out


# ---------------------------------------------------------------------------
# Transforms.
# ---------------------------------------------------------------------------


def rolling_zscore(x: np.ndarray, local_ts: np.ndarray, window: Window) -> np.ndarray:
    """Trailing-inclusive rolling zscore (population std), NaN-aware.

    NaN when: the window is incomplete (event window not yet full / time
    window reaching before the first row), fewer than ``ZSCORE_MIN_VALID``
    finite values, the current value is NaN, or the window std is 0.
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n == 0:
        return np.full(0, np.nan)
    finite = np.isfinite(x)
    vals = np.where(finite, x, 0.0)
    s1 = np.concatenate(([0.0], np.cumsum(vals)))
    s2 = np.concatenate(([0.0], np.cumsum(vals * vals)))
    cnt = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
    idx = np.arange(n)
    lo: np.ndarray
    if window.kind == "events":
        lo = idx - window.count + 1
        warm = lo < 0
        lo = np.maximum(lo, 0).astype(np.int64)
    else:
        cutoff = local_ts - window.duration_ns
        lo = np.searchsorted(local_ts, cutoff, side="right").astype(np.int64)
        warm = cutoff < local_ts[0]
    w_sum = s1[idx + 1] - s1[lo]
    w_sum2 = s2[idx + 1] - s2[lo]
    w_cnt = cnt[idx + 1] - cnt[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = w_sum / w_cnt
        var = np.maximum(w_sum2 / w_cnt - mean * mean, 0.0)
        std = np.sqrt(var)
        z = (x - mean) / std
    bad = warm | (w_cnt < ZSCORE_MIN_VALID) | ~finite | ~(std > 0.0)
    z[bad] = np.nan
    return z


def ema(x: np.ndarray, local_ts: np.ndarray, window: Window) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan)
    xs = cast("list[float]", x.tolist())
    prev = math.nan
    if window.kind == "events":
        alpha = 2.0 / (window.count + 1.0)
        for i in range(n):
            xi = xs[i]
            if xi == xi:  # finite-or-NaN gate: NaN != NaN
                prev = xi if prev != prev else prev + alpha * (xi - prev)
            out[i] = prev
    else:
        tau = float(window.duration_ns)
        ts = cast("list[int]", local_ts.tolist())
        prev_t = 0
        for i in range(n):
            xi = xs[i]
            if xi == xi:
                if prev != prev:
                    prev = xi
                else:
                    w = math.exp(-(ts[i] - prev_t) / tau)
                    prev = w * prev + (1.0 - w) * xi
                prev_t = ts[i]
            out[i] = prev
    return out


def clip(x: np.ndarray | float, lo: float, hi: float) -> np.ndarray:
    """Elementwise clip; NaN passes through unchanged."""
    return np.clip(np.asarray(x, dtype=np.float64), lo, hi)


__all__ = [
    "ZSCORE_MIN_VALID",
    "Columns",
    "book_imbalance",
    "clip",
    "depth_delta",
    "depth_sum",
    "ema",
    "future_mid_return",
    "mid_price",
    "microprice",
    "parse_canonical_window",
    "rolling_zscore",
    "safe_divide",
    "spread_ticks",
    "trade_imbalance",
    "trailing_anchor_indices",
]

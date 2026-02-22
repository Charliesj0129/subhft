"""Combinatorial alpha operator library — pure-numpy signal primitives.

All operators follow a consistent contract:
  - Accept 1-D or broadcastable arrays; coerced internally via ``_as_1d``.
  - Accept an optional ``out`` buffer (pre-allocated, same shape) for zero-alloc reuse.
  - Return ``float64`` arrays of the same length as the (possibly truncated) input.
  - Never raise on empty inputs; return zero-filled arrays instead.

``OPERATORS`` at the bottom maps string names to callables for use by the expression
language and search engine.
"""

from __future__ import annotations

from typing import Callable

import numpy as np


def ts_mean(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Rolling mean over the last *window* samples (inclusive)."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    csum = np.cumsum(arr, dtype=np.float64)
    for i in range(arr.size):
        start = max(0, i - w + 1)
        total = csum[i] - (csum[start - 1] if start > 0 else 0.0)
        target[i] = total / float(i - start + 1)
    return target


def ts_std(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Rolling standard deviation over the last *window* samples (inclusive)."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    for i in range(arr.size):
        start = max(0, i - w + 1)
        view = arr[start : i + 1]
        target[i] = np.std(view)
    return target


def ts_sum(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Rolling sum over the last *window* samples (inclusive)."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    csum = np.cumsum(arr, dtype=np.float64)
    for i in range(arr.size):
        start = max(0, i - w + 1)
        target[i] = csum[i] - (csum[start - 1] if start > 0 else 0.0)
    return target


def ts_delta(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Difference between current value and the value *window* samples ago."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    target.fill(0.0)
    if arr.size > w:
        target[w:] = arr[w:] - arr[:-w]
    return target


def ts_rank(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Percentile rank of the current value within the last *window* samples."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    for i in range(arr.size):
        start = max(0, i - w + 1)
        view = arr[start : i + 1]
        rank = float(np.sum(view <= view[-1]))
        target[i] = rank / float(view.size)
    return target


def decay_linear(x: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Linearly-weighted moving average; weights increase linearly toward the current bar."""
    arr = _as_1d(x)
    w = max(1, int(window))
    target = _target(out, arr.shape)
    for i in range(arr.size):
        start = max(0, i - w + 1)
        view = arr[start : i + 1]
        weights = np.arange(1, view.size + 1, dtype=np.float64)
        target[i] = float(np.dot(view, weights) / np.sum(weights))
    return target


def ts_corr(x: np.ndarray, y: np.ndarray, window: int, *, out: np.ndarray | None = None) -> np.ndarray:
    """Rolling Pearson correlation between *x* and *y* over the last *window* samples."""
    lhs = _as_1d(x)
    rhs = _as_1d(y)
    n = min(lhs.size, rhs.size)
    lhs = lhs[:n]
    rhs = rhs[:n]
    w = max(2, int(window))
    target = _target(out, lhs.shape)
    for i in range(n):
        start = max(0, i - w + 1)
        xv = lhs[start : i + 1]
        yv = rhs[start : i + 1]
        if xv.size < 2:
            target[i] = 0.0
            continue
        corr = np.corrcoef(xv, yv)[0, 1]
        target[i] = float(corr) if np.isfinite(corr) else 0.0
    return target


def rank(x: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Cross-sectional rank normalised to [0, 1] (0 = smallest, 1 = largest)."""
    arr = _as_1d(x)
    target = _target(out, arr.shape)
    if arr.size <= 1:
        target.fill(0.0)
        return target
    order = np.argsort(arr, kind="mergesort")
    target[order] = np.arange(arr.size, dtype=np.float64)
    target /= float(arr.size - 1)
    return target


def zscore(x: np.ndarray, window: int | None = None, *, out: np.ndarray | None = None) -> np.ndarray:
    """Z-score normalisation; rolling (over *window* bars) or full-series when *window* is None."""
    arr = _as_1d(x)
    target = _target(out, arr.shape)
    if window is None:
        mu = float(np.mean(arr))
        sigma = float(np.std(arr))
        if sigma <= 1e-12:
            target.fill(0.0)
            return target
        target[:] = (arr - mu) / sigma
        return target

    w = max(2, int(window))
    for i in range(arr.size):
        start = max(0, i - w + 1)
        view = arr[start : i + 1]
        sigma = float(np.std(view))
        target[i] = 0.0 if sigma <= 1e-12 else (arr[i] - float(np.mean(view))) / sigma
    return target


def sign(x: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Element-wise sign: −1, 0, or +1."""
    arr = _as_1d(x)
    target = _target(out, arr.shape)
    np.sign(arr, out=target)
    return target


def log1p(x: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Element-wise log(1 + x); values clipped to [−0.999999, ∞) to avoid log(0)."""
    arr = _as_1d(x)
    target = _target(out, arr.shape)
    np.log1p(np.clip(arr, a_min=-0.999999, a_max=None), out=target)
    return target


def abs_(x: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Element-wise absolute value."""
    arr = _as_1d(x)
    target = _target(out, arr.shape)
    np.abs(arr, out=target)
    return target


def add(x: np.ndarray, y: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Element-wise addition; arrays are aligned to the shorter length."""
    lhs, rhs = _align_2(x, y)
    target = _target(out, lhs.shape)
    np.add(lhs, rhs, out=target)
    return target


def mul(x: np.ndarray, y: np.ndarray, *, out: np.ndarray | None = None) -> np.ndarray:
    """Element-wise multiplication; arrays are aligned to the shorter length."""
    lhs, rhs = _align_2(x, y)
    target = _target(out, lhs.shape)
    np.multiply(lhs, rhs, out=target)
    return target


def div(x: np.ndarray, y: np.ndarray, *, out: np.ndarray | None = None, eps: float = 1e-12) -> np.ndarray:
    """Safe element-wise division; outputs 0 where |y| ≤ *eps* to avoid division by zero."""
    lhs, rhs = _align_2(x, y)
    target = _target(out, lhs.shape)
    np.divide(lhs, rhs, out=target, where=np.abs(rhs) > eps)
    target[np.abs(rhs) <= eps] = 0.0
    return target


def _target(out: np.ndarray | None, shape: tuple[int, ...]) -> np.ndarray:
    if out is None:
        return np.zeros(shape, dtype=np.float64)
    if out.shape != shape:
        raise ValueError(f"out shape mismatch: expected {shape}, got {out.shape}")
    return out


def _as_1d(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float64).reshape(-1)


def _align_2(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lhs = _as_1d(x)
    rhs = _as_1d(y)
    n = min(lhs.size, rhs.size)
    return lhs[:n], rhs[:n]


OPERATORS: dict[str, Callable[..., np.ndarray]] = {
    "abs": abs_,
    "add": add,
    "decay_linear": decay_linear,
    "div": div,
    "log1p": log1p,
    "mul": mul,
    "rank": rank,
    "sign": sign,
    "ts_corr": ts_corr,
    "ts_delta": ts_delta,
    "ts_mean": ts_mean,
    "ts_rank": ts_rank,
    "ts_std": ts_std,
    "ts_sum": ts_sum,
    "zscore": zscore,
}

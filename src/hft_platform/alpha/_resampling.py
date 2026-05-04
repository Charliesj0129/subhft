"""Resampling primitives for small-sample sub-gates.

Pure functions over `daily_pnl: list[float]` or `trades: list[float]`.
No I/O, no logging, no global state. Used by:
- LOODaySensitivityGate    -> leave_one_day_out
- OutlierTradeRemovalGate  -> drop_top_trades
- DayLevelBootstrapCIGate  -> day_bootstrap
- StationaryBlockBootstrapGate -> stationary_block_bootstrap
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Sequence

import numpy as np


def leave_one_day_out(daily_pnl: Sequence[float]) -> Iterator[list[float]]:
    """Yield N slices of daily PnL with the i-th day removed.

    Order of yielded slices matches the order of the input.
    Returns immediately if the input is empty.
    """
    n = len(daily_pnl)
    if n == 0:
        return iter([])
    return ([daily_pnl[j] for j in range(n) if j != i] for i in range(n))


def drop_top_trades(trades: Sequence[float], *, pct: float) -> list[float]:
    """Return trades with the top ``pct`` fraction (by |value|) removed.

    Args:
        trades: per-trade signed PnL.
        pct: fraction in [0, 1) to drop. ``pct=0.05`` drops the top 5%.

    Raises:
        ValueError: if pct is not in [0, 1).
    """
    if not 0.0 <= pct < 1.0:
        raise ValueError(f"pct must be in [0, 1), got {pct}")
    if not trades:
        return []
    n_drop = int(len(trades) * pct)
    if n_drop == 0:
        return list(trades)
    order = sorted(range(len(trades)), key=lambda i: abs(trades[i]), reverse=True)
    drop_idx = set(order[:n_drop])
    return [t for i, t in enumerate(trades) if i not in drop_idx]


def day_bootstrap(
    daily_pnl: Sequence[float],
    *,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Day-level non-overlapping bootstrap.

    Returns an array of shape ``(n_resamples, len(daily_pnl))`` where each row
    is a sample-with-replacement of the input days.

    Raises:
        ValueError: if fewer than 2 days are provided.
    """
    n = len(daily_pnl)
    if n < 2:
        raise ValueError(f"insufficient days for bootstrap: n={n}, need >= 2")
    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    idx = rng.integers(low=0, high=n, size=(n_resamples, n))
    return arr[idx]


def stationary_block_bootstrap(
    daily_pnl: Sequence[float],
    *,
    block_size: int,
    n_resamples: int,
    rng_seed: int,
) -> np.ndarray:
    """Politis-Romano stationary block bootstrap.

    Geometric block lengths with mean ``block_size``; concatenates blocks
    sampled with replacement from the original series until the desired
    sample length is reached. Returns ``(n_resamples, len(daily_pnl))``.

    Raises:
        ValueError: if block_size <= 0 or input shorter than block_size.
    """
    n = len(daily_pnl)
    if block_size <= 0:
        raise ValueError(f"block_size must be > 0, got {block_size}")
    if n < block_size:
        raise ValueError(f"input length {n} < block_size {block_size}")

    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    p = 1.0 / float(block_size)

    samples = np.empty((n_resamples, n), dtype=float)
    for r in range(n_resamples):
        out = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = int(rng.integers(low=0, high=n))
            length = int(rng.geometric(p=p))
            for j in range(length):
                if i >= n:
                    break
                out[i] = arr[(start + j) % n]
                i += 1
        samples[r] = out
    return samples

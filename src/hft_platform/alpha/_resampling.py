"""Resampling primitives for small-sample sub-gates.

Pure functions over `daily_pnl: list[float]` or `trades: list[float]`.
No I/O, no logging, no global state. Used by:
- LOODaySensitivityGate    -> leave_one_day_out
- OutlierTradeRemovalGate  -> drop_top_trades

The bootstraps live in ``hft_platform.alpha.stats`` with the other
resamplers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Sequence


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

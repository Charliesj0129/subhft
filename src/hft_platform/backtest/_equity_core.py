"""Shared equity computation for adapter and research runner (WU-07).

Pure numpy — no dependencies on adapter or runner internals.
"""

from __future__ import annotations

import numpy as np


def compute_equity_from_positions(
    prices: np.ndarray,
    positions: np.ndarray,
    fee_rate: float = 0.0,
    initial_equity: float = 1_000_000.0,
) -> np.ndarray:
    """Compute equity curve from prices and positions with fee deduction.

    ``pnl_step = positions[:-1] * diff(prices)`` captures mark-to-market PnL.
    Fees are proportional to turnover (absolute position change) times price.

    Args:
        prices: Mid-price array (float64).
        positions: Position array (float64).
        fee_rate: Fee rate per unit of turnover (e.g. taker_fee_bps / 10_000).
        initial_equity: Starting equity value.

    Returns:
        Equity curve array of same length as inputs.
    """
    n = min(prices.size, positions.size)
    base = float(initial_equity)
    if n < 2:
        return np.asarray([base], dtype=np.float64)

    px = prices[:n]
    pos = positions[:n]

    pnl_step = pos[:-1] * np.diff(px)
    turnover = np.abs(np.diff(pos, prepend=0))
    fee_step = turnover[1:] * np.abs(px[1:]) * fee_rate
    pnl_after_fee = pnl_step - fee_step
    pnl_cum = np.cumsum(pnl_after_fee, dtype=np.float64)

    equity = np.empty(n, dtype=np.float64)
    equity[0] = base
    equity[1:] = base + pnl_cum
    return equity

from __future__ import annotations

import numpy as np

from research.backtest.metrics import compute_cvar, compute_sharpe, compute_sortino


def test_compute_sharpe_negative_equity_excluded():
    """Returns from negative-equity ticks should be excluded, not flip signs."""
    equity = np.array([1000.0, 500.0, -100.0, 200.0])
    sharpe = compute_sharpe(equity)
    # Only the 1000->500 return (-0.5) should be used; -100 base is excluded
    assert np.isfinite(sharpe)


def test_compute_sortino_negative_equity_excluded():
    equity = np.array([1000.0, 500.0, -100.0, 200.0])
    sortino = compute_sortino(equity)
    assert np.isfinite(sortino)


def test_compute_cvar_negative_equity_excluded():
    equity = np.array([1000.0, 500.0, -100.0, 200.0])
    cvar = compute_cvar(equity)
    assert np.isfinite(cvar)


def test_compute_sharpe_all_negative_equity():
    """If all equity points are negative, return 0.0."""
    equity = np.array([-100.0, -200.0, -150.0])
    assert compute_sharpe(equity) == 0.0


def test_compute_sortino_all_negative_equity():
    equity = np.array([-100.0, -200.0, -150.0])
    assert compute_sortino(equity) == 0.0


def test_compute_cvar_all_negative_equity():
    equity = np.array([-100.0, -200.0, -150.0])
    assert compute_cvar(equity) == 0.0


def test_compute_sharpe_normal_equity_unchanged():
    """Normal positive equity should work as before."""
    equity = np.array([1000.0, 1010.0, 1005.0, 1020.0, 1015.0])
    sharpe = compute_sharpe(equity)
    assert sharpe != 0.0
    assert np.isfinite(sharpe)


def test_compute_sharpe_zero_base_excluded():
    """Zero-equity base ticks should also be excluded (division by zero)."""
    equity = np.array([1000.0, 0.0, 500.0, 600.0])
    sharpe = compute_sharpe(equity)
    assert np.isfinite(sharpe)

"""WU-07: Tests for shared equity computation in _equity_core.py."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest._equity_core import compute_equity_from_positions


def test_basic_equity_curve():
    """Long position with rising price gains equity."""
    prices = np.array([100.0, 101.0, 102.0, 103.0])
    positions = np.array([1.0, 1.0, 1.0, 1.0])
    eq = compute_equity_from_positions(prices, positions, fee_rate=0.0)
    assert eq[0] == pytest.approx(1_000_000.0)
    # PnL = 1*(101-100) + 1*(102-101) + 1*(103-102) = 3
    assert eq[-1] == pytest.approx(1_000_003.0)


def test_fee_deduction():
    """Fees reduce equity proportional to turnover * price."""
    prices = np.array([100.0, 100.0, 100.0])
    positions = np.array([0.0, 1.0, 0.0])  # buy then sell
    eq = compute_equity_from_positions(prices, positions, fee_rate=0.01)
    # Turnover: [0, 1, 1], fee at step1: 1*100*0.01=1.0, step2: 1*100*0.01=1.0
    # PnL_step: [0*(100-100), 1*(100-100)] = [0, 0]
    # After fee: [-1.0, -1.0], cum = [-1.0, -2.0]
    assert eq[-1] == pytest.approx(1_000_000.0 - 2.0)


def test_short_position():
    """Short position profits when price falls."""
    prices = np.array([100.0, 99.0, 98.0])
    positions = np.array([-1.0, -1.0, -1.0])
    eq = compute_equity_from_positions(prices, positions, fee_rate=0.0)
    # PnL: -1*(99-100) + -1*(98-99) = 1 + 1 = 2
    assert eq[-1] == pytest.approx(1_000_002.0)


def test_empty_input():
    """Single-element or empty input returns initial equity."""
    prices = np.array([100.0])
    positions = np.array([1.0])
    eq = compute_equity_from_positions(prices, positions)
    assert len(eq) == 1
    assert eq[0] == pytest.approx(1_000_000.0)


def test_custom_initial_equity():
    """Custom initial equity propagated correctly."""
    prices = np.array([100.0, 100.0])
    positions = np.array([0.0, 0.0])
    eq = compute_equity_from_positions(prices, positions, initial_equity=500.0)
    assert eq[0] == pytest.approx(500.0)
    assert eq[-1] == pytest.approx(500.0)


def test_parity_with_runner_implementation():
    """Results match the research runner's _compute_equity_curve logic."""
    np.random.seed(42)
    n = 100
    prices = np.cumsum(np.random.randn(n)) + 1000.0
    signals = np.random.randn(n)
    positions = np.clip(np.cumsum(np.sign(signals)), -5, 5).astype(np.float64)

    fee_rate = 0.002  # 20bps
    eq = compute_equity_from_positions(prices, positions, fee_rate=fee_rate)

    # Manual check: same logic
    pnl_step = positions[:-1] * np.diff(prices)
    turnover = np.abs(np.diff(positions, prepend=0))
    fee_step = turnover[1:] * np.abs(prices[1:]) * fee_rate
    expected = 1_000_000.0 + np.cumsum(pnl_step - fee_step)
    np.testing.assert_allclose(eq[1:], expected, rtol=1e-10)

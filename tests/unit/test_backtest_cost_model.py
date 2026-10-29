"""tests/unit/test_backtest_cost_model.py

Unit tests for the per-contract statutory cost in _compute_equity_curve (research v2 C2c).
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from research.backtest.cost_models import TAIFEXCost
from research.backtest.types import BacktestConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATA_PATH = "research/data/raw/dummy.npz"
_TMF = TAIFEXCost(instrument="TMF", commission_pts_per_side=0.8, tax_pts_per_side=0.45, point_value_nwd=10)


def _make_config(**kwargs) -> BacktestConfig:
    defaults = dict(
        data_paths=[_BASE_DATA_PATH],
        cost_per_side_pts=_TMF.cost_per_side_pts,
        initial_equity=0.0,
    )
    defaults.update(kwargs)
    return BacktestConfig(**defaults)


def _run_equity(prices, positions, config):
    """Import and call _compute_equity_curve directly."""
    from research.backtest.hft_native_runner import _compute_equity_curve

    return _compute_equity_curve(
        np.asarray(prices, dtype=np.float64),
        np.asarray(positions, dtype=np.float64),
        config,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bps_and_rebate_fields_are_gone():
    names = {f.name for f in dataclasses.fields(BacktestConfig)}
    assert not names & {"maker_fee_bps", "taker_fee_bps", "sell_tax_bps"}
    assert BacktestConfig(data_paths=[_BASE_DATA_PATH]).cost_per_side_pts is None  # gross screening


def test_round_trip_costs_two_sides_of_the_cost_profile():
    prices = np.full(3, 22_000.0)
    eq = _run_equity(prices, [0.0, 1.0, 0.0], _make_config())
    assert -(eq[-1] - eq[0]) == pytest.approx(_TMF.rt_cost_pts)


def test_buys_and_sells_cost_the_same():
    """TAIFEX futures tax both sides, so a long and a short round trip cost the same."""
    prices = np.full(3, 22_000.0)
    long_rt = _run_equity(prices, [0.0, 1.0, 0.0], _make_config())
    short_rt = _run_equity(prices, [0.0, -1.0, 0.0], _make_config())
    assert long_rt[-1] == pytest.approx(short_rt[-1])


def test_cost_scales_with_contracts_not_price():
    cfg = _make_config()
    low = _run_equity(np.full(3, 100.0), [0.0, 3.0, 0.0], cfg)
    high = _run_equity(np.full(3, 30_000.0), [0.0, 3.0, 0.0], cfg)
    assert -low[-1] == pytest.approx(6 * _TMF.cost_per_side_pts)
    assert high[-1] == pytest.approx(low[-1])


def test_gross_config_charges_nothing():
    eq = _run_equity(np.full(3, 22_000.0), [0.0, 1.0, 0.0], _make_config(cost_per_side_pts=None))
    np.testing.assert_array_equal(eq, np.zeros(3))


def test_no_position_no_fee():
    """A flat-zero position series should incur zero fees and a flat equity curve."""
    cfg = _make_config(initial_equity=500_000.0)
    eq = _run_equity([100.0, 101.0, 102.0], [0.0, 0.0, 0.0], cfg)
    np.testing.assert_array_almost_equal(eq, np.full(3, 500_000.0), decimal=10)


def test_short_series_returns_base():
    """A price series shorter than 2 should return [initial_equity]."""
    result = _run_equity([100.0], [1.0], _make_config(initial_equity=999_000.0))
    assert result.shape == (1,)
    assert result[0] == 999_000.0

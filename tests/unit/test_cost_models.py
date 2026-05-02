"""Tests for TAIFEX cost model."""

import pytest

from research.backtest.cost_models import TAIFEXCost, load_cost_profile


@pytest.fixture(autouse=True)
def _reset_cost_cache():
    """Reset the module-level cache between tests to ensure isolation."""
    import research.backtest.cost_models as _mod

    _mod._cache = None
    yield
    _mod._cache = None


def test_load_tmfd6_cost():
    cost = load_cost_profile("TMFD6")
    assert isinstance(cost, TAIFEXCost)
    assert cost.commission_pts_per_side == 1.3
    assert cost.tax_pts_per_side == 0.7
    assert cost.point_value_nwd == 10


def test_load_txfd6_cost():
    cost = load_cost_profile("TXFD6")
    assert cost.commission_pts_per_side == 0.3
    assert cost.point_value_nwd == 200


def test_rt_cost_pts():
    cost = load_cost_profile("TMFD6")
    assert cost.rt_cost_pts == 4.0


def test_apply_fill_cost():
    cost = load_cost_profile("TMFD6")
    net = cost.apply(gross_pnl_pts=10.0, n_fills=2)
    assert net == 6.0


def test_cost_model_label():
    cost = load_cost_profile("TMFD6")
    assert cost.label == "TMFD6(comm=1.3,tax=0.7)"


def test_unknown_instrument_raises():
    with pytest.raises(KeyError, match="UNKNOWN"):
        load_cost_profile("UNKNOWN")

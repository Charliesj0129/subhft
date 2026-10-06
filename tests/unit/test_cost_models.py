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


# Index-futures tax is 0.002% of notional on BOTH sides: price x 0.00002 points per side,
# modelled at a 50,000 reference = 1.0 pt for TXF, MXF and TMF alike. The earlier file charged
# TXF 1.2 / TMF 0.7 pt (tax treated as sell-only), which made TMF 1.0 pt per side too cheap.
# Commission defaults to the high end of the broker range (NTD/side: TXF 100, MXF 50, TMF 20).
OLD_COST_PER_SIDE_PTS = {"TXF": 1.5, "TMF": 2.0}


@pytest.mark.parametrize(
    ("instrument", "commission", "tax", "point_value", "per_side"),
    [
        ("TXF", 0.5, 1.0, 200, 1.5),
        ("MXF", 1.0, 1.0, 50, 2.0),
        ("TMF", 2.0, 1.0, 10, 3.0),
    ],
)
def test_root_profiles_charge_the_statutory_tax_on_both_sides(instrument, commission, tax, point_value, per_side):
    cost = load_cost_profile(instrument)

    assert isinstance(cost, TAIFEXCost)
    assert cost.commission_pts_per_side == commission
    assert cost.tax_pts_per_side == tax
    assert cost.point_value_nwd == point_value
    assert cost.cost_per_side_pts == per_side
    assert cost.rt_cost_pts == 2 * per_side


@pytest.mark.parametrize("instrument", ["TMFD6", "TMFB6", "TMFC6", "TMFE6", "TMFF6", "TMFG6", "TMFH6"])
def test_every_listed_tmf_month_costs_the_root_default(instrument):
    cost = load_cost_profile(instrument)

    assert cost.cost_per_side_pts == load_cost_profile("TMF").cost_per_side_pts == 3.0
    assert cost.point_value_nwd == 10
    assert cost.rt_cost_pts == 6.0


@pytest.mark.parametrize("instrument", ["TXFD6", "TXFB6", "TXFC6", "TXFE6", "TXFF6", "TXFG6", "TXFH6"])
def test_every_listed_txf_month_costs_the_root_default(instrument):
    cost = load_cost_profile(instrument)

    assert cost.cost_per_side_pts == 1.5
    assert cost.point_value_nwd == 200
    assert cost.rt_cost_pts == 3.0
    assert cost.scale == 1_000_000


@pytest.mark.parametrize("instrument", ["TMFI6", "TMFJ6", "TMFL6", "TMFA7", "TXFJ6", "TXFK6", "MXFJ6"])
def test_an_unlisted_contract_month_falls_back_to_its_root(instrument):
    cost = load_cost_profile(instrument)

    assert cost.instrument == instrument
    assert cost.cost_per_side_pts == load_cost_profile(instrument[:3]).cost_per_side_pts


@pytest.mark.parametrize("instrument", ["TMFM6", "TMFJ", "TMFJ66", "tmfj6", "XTMFJ6", "TXOJ6", "TMF_low6"])
def test_a_code_that_is_not_a_contract_month_does_not_fall_back(instrument):
    with pytest.raises(KeyError, match="No cost profile"):
        load_cost_profile(instrument)


@pytest.mark.parametrize("root", ["TXF", "MXF", "TMF"])
def test_sensitivity_profiles_are_cheaper_than_the_default_and_ordered(root):
    low, mid, default = (load_cost_profile(name).cost_per_side_pts for name in (f"{root}_low", f"{root}_mid", root))

    assert low < mid < default
    assert load_cost_profile(f"{root}_low").tax_pts_per_side == load_cost_profile(root).tax_pts_per_side


@pytest.mark.parametrize("instrument", ["TXFD6", "TXFH6", "TMFD6", "TMFH6"])
def test_no_existing_profile_became_cheaper(instrument):
    old = OLD_COST_PER_SIDE_PTS[instrument[:3]]

    assert load_cost_profile(instrument).cost_per_side_pts >= old


def test_rt_cost_pts():
    cost = load_cost_profile("TMFD6")
    assert cost.rt_cost_pts == 6.0


def test_apply_fill_cost():
    cost = load_cost_profile("TMFD6")
    net = cost.apply(gross_pnl_pts=10.0, n_fills=2)
    assert net == 4.0


def test_cost_model_label():
    cost = load_cost_profile("TMFD6")
    assert cost.label == "TMFD6(comm=2.0,tax=1.0)"


def test_unknown_instrument_raises():
    with pytest.raises(KeyError, match="UNKNOWN"):
        load_cost_profile("UNKNOWN")

"""The statistical day-axis sub-gates must count days, not ``daily_pnl`` rows.

Companion to ``test_sub_gate_day_axis_aggregation``, which covers the two
concentration gates.  The gates here all derive a *sample size* from
``daily_pnl`` -- ``n_days``, ``oos_len``, a block length in days -- and every
one of them is fail-open when rows are counted instead of dates:

* ``sharpe_threshold`` / ``deflated_sharpe_maker``: splitting a day across
  contracts halves each observation but doubles ``n``, and the deflation
  penalty ``sqrt(2*ln(trials)/oos_len)`` *shrinks* as ``oos_len`` grows;
* ``winning_day_pct`` counts a day that won on one contract and lost on
  another as one win and one loss instead of one net day;
* ``max_drawdown`` walks the equity curve, so intra-date rows fabricate a
  path that never existed;
* ``day_bootstrap_ci`` / ``stationary_block_bootstrap`` resample the series,
  so a 5-*day* block becomes ~1.25 real days and the interval narrows;
* ``cost_uncertainty`` gates on ``n_days >= min_days_strict`` and divides by
  ``sqrt(n_days)``.

Every test below contrasts a two-contract result against the single-contract
result it aggregates to; the two must agree.
"""

from __future__ import annotations

from types import SimpleNamespace

from hft_platform.alpha._sub_gates.common import (
    MaxDrawdownGate,
    SharpeThresholdGate,
    WinningDayPctGate,
)
from hft_platform.alpha._sub_gates.cost_uncertainty import CostUncertaintyGate
from hft_platform.alpha._sub_gates.day_bootstrap_ci import DayLevelBootstrapCIGate
from hft_platform.alpha._sub_gates.deflated_sharpe_maker import DeflatedSharpeForMakerGate
from hft_platform.alpha._sub_gates.stationary_block_bootstrap import StationaryBlockBootstrapGate

_DATES = [f"2026-05-{d:02d}" for d in range(1, 11)]
_DAY_PNL = [12.0, -4.0, 20.0, 6.0, -9.0, 15.0, 3.0, -2.0, 11.0, 7.0]


def _result(rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(daily_pnl=rows)


def _one_row_per_day(fills: int = 4) -> list[dict]:
    return [{"date": d, "pnl_pts": p, "fills": fills} for d, p in zip(_DATES, _DAY_PNL, strict=True)]


def _split_across_two_contracts(fills: int = 2) -> list[dict]:
    """The same ten days, each split over two contracts. Same days, twice the rows."""
    rows: list[dict] = []
    for d, p in zip(_DATES, _DAY_PNL, strict=True):
        rows.append({"date": d, "pnl_pts": p * 0.75, "fills": fills})
        rows.append({"date": d, "pnl_pts": p * 0.25, "fills": fills})
    return rows


def test_sharpe_is_computed_over_days_not_over_contract_rows() -> None:
    gate = SharpeThresholdGate()
    whole = gate.evaluate(_result(_one_row_per_day()), None, {})
    split = gate.evaluate(_result(_split_across_two_contracts()), None, {})
    assert split.metrics["sharpe"] == whole.metrics["sharpe"]


def test_winning_day_pct_counts_a_split_day_once() -> None:
    """A day up on one contract and down on the other is one winning day."""
    rows = [
        {"date": "2026-05-01", "pnl_pts": 30.0},
        {"date": "2026-05-01", "pnl_pts": -10.0},
        {"date": "2026-05-02", "pnl_pts": -8.0},
    ]
    result = WinningDayPctGate().evaluate(_result(rows), None, {})
    # Two days, one of them net positive -> 50%. Row-counting gives 1/3 = 33%.
    assert result.metrics["winning_day_pct"] == 50.0


def test_max_drawdown_walks_the_day_equity_curve_not_a_fabricated_intraday_path() -> None:
    """Rows within a date have no ordering, so they must not create a trough."""
    rows = [
        {"date": "2026-05-01", "pnl_pts": 100.0},
        # Same day: a -60 row ordered after +40 fabricates a drawdown that the
        # day (net -20) never had as a separate low point.
        {"date": "2026-05-02", "pnl_pts": 40.0},
        {"date": "2026-05-02", "pnl_pts": -60.0},
        {"date": "2026-05-03", "pnl_pts": 30.0},
    ]
    aggregated = [
        {"date": "2026-05-01", "pnl_pts": 100.0},
        {"date": "2026-05-02", "pnl_pts": -20.0},
        {"date": "2026-05-03", "pnl_pts": 30.0},
    ]
    gate = MaxDrawdownGate()
    assert (
        gate.evaluate(_result(rows), None, {}).metrics["max_dd_pct"]
        == (gate.evaluate(_result(aggregated), None, {}).metrics["max_dd_pct"])
    )


def test_day_bootstrap_reports_the_real_day_count() -> None:
    gate = DayLevelBootstrapCIGate()
    split = gate.evaluate(_result(_split_across_two_contracts()), None, {})
    assert split.metrics["n_days"] == float(len(_DATES))


def test_block_bootstrap_block_length_spans_real_days() -> None:
    """A block_size of 5 must cover 5 dates, not 5 rows (2.5 dates)."""
    gate = StationaryBlockBootstrapGate()
    whole = gate.evaluate(_result(_one_row_per_day()), None, {})
    split = gate.evaluate(_result(_split_across_two_contracts()), None, {})
    assert split.metrics["n_days"] == whole.metrics["n_days"] == float(len(_DATES))
    assert split.metrics["ci_lower"] == whole.metrics["ci_lower"]


def test_deflation_penalty_is_not_diluted_by_extra_contract_rows() -> None:
    """More rows must not shrink the multiple-testing penalty."""
    gate = DeflatedSharpeForMakerGate()
    thresholds = {"deflated_n_trials": 100}
    whole = gate.evaluate(_result(_one_row_per_day()), None, thresholds)
    split = gate.evaluate(_result(_split_across_two_contracts()), None, thresholds)
    assert split.metrics["n_days"] == float(len(_DATES))
    assert split.metrics["penalty"] == whole.metrics["penalty"]


def test_cost_uncertainty_minimum_sample_is_not_cleared_by_splitting_days() -> None:
    """Four real days split over two contracts is still four days, not eight."""
    rows: list[dict] = []
    for d, p in zip(_DATES[:4], _DAY_PNL[:4], strict=True):
        rows.append({"date": d, "pnl_pts": p * 0.5, "fills": 3})
        rows.append({"date": d, "pnl_pts": p * 0.5, "fills": 3})
    result = CostUncertaintyGate().evaluate(
        _result(rows),
        None,
        {
            "_is_strict_profile": True,
            "cost_uncertainty_min_days_strict": 5,
            "cost_uncertainty_p95_lower_bound_min_pts": 0.0,
        },
    )
    assert result.metrics["n_days"] == 4
    assert result.passed is False


def test_cost_uncertainty_still_drops_non_traded_rows_when_aggregating() -> None:
    """A date whose only rows have zero fills is not a traded day."""
    rows = [
        {"date": "2026-05-01", "pnl_pts": 10.0, "fills": 2},
        {"date": "2026-05-02", "pnl_pts": 0.0, "fills": 0},
        {"date": "2026-05-03", "pnl_pts": 4.0, "fills": 0},
        {"date": "2026-05-03", "pnl_pts": 6.0, "fills": 1},
    ]
    result = CostUncertaintyGate().evaluate(_result(rows), None, {"cost_uncertainty_p95_lower_bound_min_pts": 0.0})
    # 05-01 and 05-03 traded; 05-02 did not. 05-03 keeps only its filled row.
    assert result.metrics["n_days"] == 2
    assert result.metrics["mean_daily_pnl_pts"] == 8.0

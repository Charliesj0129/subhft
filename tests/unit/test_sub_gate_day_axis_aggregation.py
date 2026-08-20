"""Day-axis sub-gates must ask their question about days, not about rows.

``BacktestResult.daily_pnl`` is one row per (instrument, date).  A single
instrument yields one row per date, which is why this has never bitten the
governed Gate-C path -- ``research/backtest/maker_engine`` loops dates for one
instrument.  A result that concatenates instruments contributes several rows
for the same date, and then:

* ``single_day_dominance`` divides the top *row* by the total, so a day whose
  PnL is split across two contracts looks like two smaller days and the gate
  under-reports concentration;
* ``loo_day_sensitivity`` removes one *row*, so a dominant day split across two
  contracts can never be fully removed and the sign always survives.

Both directions are fail-open: the gate passes something it exists to reject.
"""

from __future__ import annotations

from types import SimpleNamespace

from hft_platform.alpha._sub_gates.loo_day_sensitivity import LOODaySensitivityGate
from hft_platform.alpha._sub_gates.single_day_dominance import SingleDayDominanceGate


def _result(rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(daily_pnl=rows)


def _split_dominant_day() -> list[dict]:
    """One day carries the whole edge, split across two contracts.

    Day 03 totals 900 of a 1000-point |total|, i.e. 90% concentration. Split
    into 450 + 450 it reads as two 45% rows.
    """
    return [
        {"date": "2026-05-01", "pnl_pts": 50.0},
        {"date": "2026-05-02", "pnl_pts": 50.0},
        {"date": "2026-05-03", "pnl_pts": 450.0},
        {"date": "2026-05-03", "pnl_pts": 450.0},
    ]


def test_single_day_dominance_measures_the_whole_day_not_one_contract_slice() -> None:
    gate = SingleDayDominanceGate()
    res = gate.evaluate(_result(_split_dominant_day()), None, {"outlier_day_contribution_max_pct": 60.0})

    assert res.metrics["n_days"] == 3.0, "four rows span three trading dates"
    assert res.metrics["top_day_contribution_pct"] == 90.0
    assert res.passed is False, "a 90% day must fail a 60% threshold even when split across contracts"


def test_loo_day_sensitivity_removes_a_whole_day_not_one_contract_slice() -> None:
    """Total is +100 only because of one +900 day sitting on -800 of losses."""
    rows = [
        {"date": "2026-05-01", "pnl_pts": -400.0},
        {"date": "2026-05-02", "pnl_pts": -400.0},
        {"date": "2026-05-03", "pnl_pts": 450.0},
        {"date": "2026-05-03", "pnl_pts": 450.0},
    ]
    gate = LOODaySensitivityGate()
    res = gate.evaluate(_result(rows), None, {"loo_day_sign_preserved": True})

    assert res.metrics["n_days"] == 3.0
    assert res.passed is False, "removing the whole dominant day flips the sign, so the gate must fail"


def test_one_row_per_date_is_unchanged() -> None:
    """The single-instrument shape every current result has must be untouched."""
    rows = [
        {"date": "2026-05-01", "pnl_pts": 50.0},
        {"date": "2026-05-02", "pnl_pts": 50.0},
        {"date": "2026-05-03", "pnl_pts": 900.0},
    ]
    dom = SingleDayDominanceGate().evaluate(_result(rows), None, {"outlier_day_contribution_max_pct": 60.0})
    assert dom.metrics["n_days"] == 3.0
    assert dom.metrics["top_day_contribution_pct"] == 90.0
    assert dom.passed is False


def test_undated_float_rows_keep_their_one_row_per_day_meaning() -> None:
    """Legacy ``list[float]`` payloads have no date; each entry is still a day."""
    rows = [50.0, 50.0, 900.0]
    dom = SingleDayDominanceGate().evaluate(_result(rows), None, {"outlier_day_contribution_max_pct": 60.0})
    assert dom.metrics["n_days"] == 3.0
    assert dom.metrics["top_day_contribution_pct"] == 90.0


def test_dated_days_are_evaluated_in_chronological_order() -> None:
    """Aggregation must not reorder days -- drawdown-style reads depend on it."""
    rows = [
        {"date": "2026-05-03", "pnl_pts": 30.0},
        {"date": "2026-05-01", "pnl_pts": 10.0},
        {"date": "2026-05-02", "pnl_pts": 20.0},
    ]
    from hft_platform.alpha._sub_gates.common import _to_daily_series

    assert _to_daily_series(rows) == [10.0, 20.0, 30.0]

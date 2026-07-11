"""Bear/high-vol regime classification and subset-filtering for the vwap_fade OOS check."""

from __future__ import annotations

import math

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import Bars
from research.experiments.validations.of_vwap_dev_luxalgo_v0.of_vwap_bear_regime_oos import (
    _classify_regime,
    _consecutive_negative_runs,
    _daily_stats,
    _dates_by_contract,
    _pooled_subset,
)


def _bars(*, open_: list[float], close: list[float], date: list[str], is_close: list[bool]) -> Bars:
    n = len(close)
    return Bars(
        open=np.array(open_, dtype=float),
        high=np.array(close, dtype=float),
        low=np.array(close, dtype=float),
        close=np.array(close, dtype=float),
        volume=np.ones(n, dtype=float),
        date=np.array(date, dtype=object),
        is_session_close=np.array(is_close, dtype=bool),
        contract="txftest",
    )


def _row(contract: str, date: str, ret_pts: float, rv_pts: float, n_bars: int = 10) -> dict:
    return {"contract": contract, "date": date, "ret_pts": ret_pts, "rv_pts": rv_pts, "n_bars": n_bars}


def test_daily_stats_computes_open_close_return_and_realized_vol_per_day():
    bars = _bars(
        open_=[100.0, 105.0],
        close=[105.0, 110.0],
        date=["2026-01-01", "2026-01-01"],
        is_close=[False, True],
    )

    rows = _daily_stats(bars, "txfd6")

    assert len(rows) == 1
    row = rows[0]
    assert row["contract"] == "txfd6"
    assert row["date"] == "2026-01-01"
    assert row["open"] == 100.0
    assert row["close"] == 110.0
    assert row["ret_pts"] == 10.0
    expected_rv = abs(math.log(110.0 / 105.0)) * 100.0
    assert row["rv_pts"] == round(expected_rv, 1)
    assert row["n_bars"] == 2


def test_daily_stats_splits_days_on_session_close_flag_not_bar_count():
    bars = _bars(
        open_=[100.0, 101.0, 102.0, 200.0, 201.0],
        close=[101.0, 102.0, 103.0, 201.0, 202.0],
        date=["2026-01-01", "2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"],
        is_close=[False, False, True, False, True],
    )

    rows = _daily_stats(bars, "txfd6")

    assert [r["date"] for r in rows] == ["2026-01-01", "2026-01-02"]
    assert rows[0]["n_bars"] == 3
    assert rows[0]["open"] == 100.0
    assert rows[0]["close"] == 103.0
    assert rows[1]["n_bars"] == 2
    assert rows[1]["open"] == 200.0
    assert rows[1]["close"] == 202.0


def test_classify_regime_flags_genuine_bear_when_bottom_quartile_is_actually_negative():
    rows = [
        _row("c", f"2026-01-{d:02d}", ret_pts, 300.0)
        for d, ret_pts in enumerate([500, 400, 300, 200, 100, -50, -300, -600], start=1)
    ]

    regime = _classify_regime(rows)

    assert regime["genuine_bear_regime_found"] is True
    assert regime["return_pts_dist"]["q25"] < 0
    assert any(r["ret_pts"] < 0 for r in regime["bear_rows"])


def test_classify_regime_does_not_force_bear_label_on_all_bullish_data():
    # Every day is positive -- the bottom quartile is merely "less bullish",
    # not an actual down day. Must NOT be reported as a genuine bear regime.
    rows = [
        _row("c", f"2026-01-{d:02d}", ret_pts, 300.0)
        for d, ret_pts in enumerate([50, 100, 150, 200, 250, 300, 350, 400], start=1)
    ]

    regime = _classify_regime(rows)

    assert regime["genuine_bear_regime_found"] is False
    assert regime["return_pts_dist"]["q25"] >= 0


def test_classify_regime_flags_genuine_highvol_when_top_quartile_meaningfully_elevated():
    rows = [
        _row("c", f"2026-01-{d:02d}", 0.0, rv) for d, rv in enumerate([200, 210, 220, 230, 240, 250, 800, 900], start=1)
    ]

    regime = _classify_regime(rows)

    assert regime["genuine_highvol_regime_found"] is True
    assert regime["rv_pts_dist"]["q75"] >= 1.25 * regime["rv_pts_dist"]["median"]


def test_classify_regime_does_not_force_highvol_label_on_flat_dispersion():
    # RV barely varies day to day -- the top quartile is not meaningfully
    # more volatile than the median, so it must not be flagged as genuine.
    rows = [
        _row("c", f"2026-01-{d:02d}", 0.0, rv) for d, rv in enumerate([295, 298, 300, 302, 305, 308, 310, 312], start=1)
    ]

    regime = _classify_regime(rows)

    assert regime["genuine_highvol_regime_found"] is False


def test_consecutive_negative_runs_detects_run_length_and_bounds():
    rows = [
        _row("c", "2026-01-01", -10, 100),
        _row("c", "2026-01-02", -20, 100),
        _row("c", "2026-01-03", -30, 100),
        _row("c", "2026-01-04", 5, 100),
        _row("c", "2026-01-05", -1, 100),
    ]

    runs = _consecutive_negative_runs(rows)

    assert runs["n_merged_calendar_days"] == 5
    assert runs["max_consecutive_negative_run"] == 3
    assert runs["runs_ge_2"] == [{"start": "2026-01-01", "end": "2026-01-03", "n_days": 3}]


def test_consecutive_negative_runs_dedupes_overlapping_contract_dates_by_bar_count():
    # Same calendar date from two concurrently-traded contracts: the row with
    # more bars (more reliable) must win the dedup, not an arbitrary one.
    rows = [
        _row("txfb6", "2026-01-01", -50, 100, n_bars=57),
        _row("txfd6", "2026-01-01", 50, 100, n_bars=10),  # sparse day, should lose
    ]

    runs = _consecutive_negative_runs(rows)

    assert runs["n_merged_calendar_days"] == 1
    assert runs["max_consecutive_negative_run"] == 0  # the winning (txfb6) row is negative alone -> no run of >=2


def test_dates_by_contract_groups_dates_under_their_own_contract_only():
    rows = [
        _row("txfb6", "2026-01-01", -10, 100),
        _row("txfb6", "2026-01-02", -10, 100),
        _row("txfd6", "2026-01-01", -10, 100),
    ]

    grouped = _dates_by_contract(rows)

    assert grouped == {"txfb6": {"2026-01-01", "2026-01-02"}, "txfd6": {"2026-01-01"}}


def test_pooled_subset_only_keeps_trades_on_that_contracts_own_regime_dates():
    trades_by_contract = {
        "txfb6": [{"date": "2026-01-01", "gross": 5.0}, {"date": "2026-01-02", "gross": -5.0}],
        "txfd6": [{"date": "2026-01-01", "gross": 7.0}],
    }
    # txfd6's regime date (2026-01-01) must not leak into txfb6's filter, and
    # vice versa -- each contract is judged only by its own regime days.
    dates_by_contract = {"txfb6": {"2026-01-02"}, "txfd6": set()}

    pooled = _pooled_subset(trades_by_contract, dates_by_contract)

    assert pooled == [{"date": "2026-01-02", "gross": -5.0}]


def test_pooled_subset_returns_empty_when_no_contract_has_regime_dates():
    trades_by_contract = {"txfb6": [{"date": "2026-01-01", "gross": 5.0}]}
    dates_by_contract: dict[str, set[str]] = {}

    pooled = _pooled_subset(trades_by_contract, dates_by_contract)

    assert pooled == []

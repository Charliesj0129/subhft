from __future__ import annotations

import numpy as np

from research.experiments.validations.t1g_extreme_imbalance_v0.diagnostic import (
    BboQuote,
    DecisionFeature,
    assign_prior_date_branches,
    branch_label_scorecard,
    branch_scorecard,
    executable_label_from_quotes,
    load_target_bbo_quotes_from_hftbt_npz,
    signed_trade_imbalance,
)
from research.experiments.validations.t1g_extreme_imbalance_v0.regime_review import (
    classify_market_regime,
    regime_split_scorecard,
)
from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_EVENT,
    DEPTH_SNAPSHOT_EVENT,
    EXCH_EVENT,
    LOCAL_EVENT,
    SELL_EVENT,
)

TICK_DTYPE = np.dtype(
    [
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("price", "<f8"),
        ("price_scaled", "<i8"),
        ("qty", "<f8"),
        ("side", "i1"),
    ]
)


def _ticks(rows: list[tuple[int, float, float, int]]) -> np.ndarray:
    return np.array(
        [(ts, ts, price, int(price * 1_000_000), qty, side) for ts, price, qty, side in rows],
        dtype=TICK_DTYPE,
    )


def test_signed_trade_imbalance_uses_only_pre_entry_window():
    ticks = _ticks(
        [
            (10, 100.0, 1.0, 1),
            (20, 101.0, 3.0, -1),
            (30, 102.0, 1000.0, 1),
        ]
    )

    feature = signed_trade_imbalance(ticks, start_ns=0, end_ns=30)

    assert feature.tick_count == 2
    assert feature.signed_imbalance == -0.5
    assert feature.return_pts == 1.0


def test_assign_prior_date_branches_does_not_use_same_day_thresholds():
    rows = [
        DecisionFeature("TXFD6", "2026-04-01", 100, 0.10, 1.0, 10.0, 10),
        DecisionFeature("TXFD6", "2026-04-02", 100, 0.20, 2.0, 10.0, 10),
        DecisionFeature("TXFD6", "2026-04-03", 100, 0.30, 3.0, 10.0, 10),
        DecisionFeature("TXFD6", "2026-04-04", 100, 0.40, 4.0, 10.0, 10),
        DecisionFeature("TXFD6", "2026-04-05", 100, 99.00, 99.0, 10.0, 10),
    ]

    assigned = assign_prior_date_branches(rows, min_prior_rows=4)

    tradable = [row for row in assigned if row["branch"] != "insufficient_prior"]
    assert len(tradable) == 1
    assert tradable[0]["date"] == "2026-04-05"
    assert tradable[0]["thresholds"]["imbalance_q90"] == 0.37
    assert tradable[0]["thresholds"]["return_q70"] == 3.1
    assert tradable[0]["branch"] == "extreme_high_imbalance_momentum"


def test_branch_scorecard_reports_branches_separately():
    assigned = [
        {"branch": "extreme_high_imbalance_momentum", "date": "2026-04-05"},
        {"branch": "extreme_high_imbalance_momentum", "date": "2026-04-06"},
        {"branch": "extreme_low_imbalance_reversal", "date": "2026-04-07"},
        {"branch": "none", "date": "2026-04-08"},
    ]

    scorecard = branch_scorecard(assigned)

    assert scorecard["candidate_events"] == 3
    assert scorecard["branches"]["extreme_high_imbalance_momentum"]["events"] == 2
    assert scorecard["branches"]["extreme_low_imbalance_reversal"]["events"] == 1
    assert scorecard["branches"]["none"]["events"] == 1


def test_executable_label_from_quotes_crosses_tmf_bid_ask_and_costs():
    minute = 60_000_000_000
    quotes = [
        BboQuote(10 * minute, 99.0, 101.0, 3.0, 4.0),
        BboQuote(20 * minute, 110.0, 112.0, 5.0, 6.0),
    ]

    long_label = executable_label_from_quotes(
        quotes,
        decision_time_ns=15 * minute,
        direction=1,
        horizon_minutes=5,
        round_trip_cost_pts=8.0,
    )
    short_label = executable_label_from_quotes(
        quotes,
        decision_time_ns=15 * minute,
        direction=-1,
        horizon_minutes=5,
        round_trip_cost_pts=8.0,
    )

    assert long_label is not None
    assert long_label.gross_pts == 9.0
    assert long_label.net_pts == 1.0
    assert long_label.entry_spread_pts == 2.0
    assert long_label.exit_spread_pts == 2.0

    assert short_label is not None
    assert short_label.gross_pts == -13.0
    assert short_label.net_pts == -21.0


def test_branch_label_scorecard_reports_remove_best_by_horizon():
    rows = [
        {"branch": "extreme_high_imbalance_momentum", "date": "2026-04-01", "label_5m_net_pts": 1.0},
        {"branch": "extreme_high_imbalance_momentum", "date": "2026-04-02", "label_5m_net_pts": 3.0},
        {"branch": "extreme_low_imbalance_reversal", "date": "2026-04-03", "label_5m_net_pts": -2.0},
        {"branch": "none", "date": "2026-04-04", "label_5m_net_pts": 99.0},
    ]

    scorecard = branch_label_scorecard(rows, horizons_minutes=(5,))

    assert scorecard["candidate_labeled_events"] == 3
    high = scorecard["branches"]["extreme_high_imbalance_momentum"]["horizons"]["5m"]
    assert high["events"] == 2
    assert high["mean_net_pts"] == 2.0
    assert high["remove_best_mean_net_pts"] == 1.0
    low = scorecard["branches"]["extreme_low_imbalance_reversal"]["horizons"]["5m"]
    assert low["events"] == 1
    assert low["remove_best_mean_net_pts"] is None


def test_load_target_bbo_quotes_uses_latest_prior_snapshot(tmp_path):
    dtype = np.dtype(
        [
            ("ev", "<u8"),
            ("exch_ts", "<i8"),
            ("local_ts", "<i8"),
            ("px", "<f8"),
            ("qty", "<f8"),
            ("order_id", "<u8"),
            ("ival", "<i8"),
            ("fval", "<f8"),
        ]
    )
    data = np.array(
        [
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, 10, 10, 99.0, 1.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, 10, 10, 101.0, 1.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, 20, 20, 100.0, 1.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, 20, 20, 101.0, 0.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, 20, 20, 102.0, 1.0, 0, 0, 0.0),
        ],
        dtype=dtype,
    )
    path = tmp_path / "sample.hftbt.npz"
    np.savez(path, data=data)

    quotes = load_target_bbo_quotes_from_hftbt_npz(path, target_ts_ns=[5, 15, 20, 25])

    assert 5 not in quotes
    assert quotes[15].bid == 99.0
    assert quotes[15].ask == 101.0
    assert quotes[20].bid == 100.0
    assert quotes[25].ask == 102.0


def test_load_target_bbo_quotes_accepts_hftbt_snapshot_events(tmp_path):
    dtype = np.dtype(
        [
            ("ev", "<u8"),
            ("exch_ts", "<i8"),
            ("local_ts", "<i8"),
            ("px", "<f8"),
            ("qty", "<f8"),
            ("order_id", "<u8"),
            ("ival", "<i8"),
            ("fval", "<f8"),
        ]
    )
    data = np.array(
        [
            (DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, 10, 10, 99.0, 1.0, 0, 0, 0.0),
            (DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, 10, 10, 101.0, 1.0, 0, 0, 0.0),
        ],
        dtype=dtype,
    )
    path = tmp_path / "snapshot.hftbt.npz"
    np.savez(path, data=data)

    quotes = load_target_bbo_quotes_from_hftbt_npz(path, target_ts_ns=[10])

    assert quotes[10].bid == 99.0
    assert quotes[10].ask == 101.0


def test_classify_market_regime_uses_pre_entry_and_entry_state_only():
    minute = 60_000_000_000
    row = {
        "date": "2026-04-01",
        "decision_time_ns": 1775004300000000000 + 45 * minute,
        "return_pts": 125.0,
        "label_5m_entry_spread_pts": 2.0,
    }

    regime = classify_market_regime(row)

    assert regime["time_bucket"] == "opening_0_60m"
    assert regime["txf_move_bucket"] == "large_up_ge_100"
    assert regime["tmf_spread_bucket"] == "tight_le_2"


def test_regime_split_scorecard_requires_remove_best_survival():
    rows = [
        {
            "branch": "extreme_low_imbalance_reversal",
            "date": "2026-04-01",
            "time_bucket": "opening_0_60m",
            "label_30m_net_pts": 1.0,
        },
        {
            "branch": "extreme_low_imbalance_reversal",
            "date": "2026-04-02",
            "time_bucket": "opening_0_60m",
            "label_30m_net_pts": 3.0,
        },
        {
            "branch": "extreme_low_imbalance_reversal",
            "date": "2026-04-03",
            "time_bucket": "opening_0_60m",
            "label_30m_net_pts": 5.0,
        },
        {
            "branch": "extreme_high_imbalance_momentum",
            "date": "2026-04-04",
            "time_bucket": "mid_60_180m",
            "label_30m_net_pts": -4.0,
        },
        {
            "branch": "extreme_high_imbalance_momentum",
            "date": "2026-04-05",
            "time_bucket": "mid_60_180m",
            "label_30m_net_pts": 10.0,
        },
    ]

    scorecard = regime_split_scorecard(rows, dimension="time_bucket", horizons_minutes=(30,), min_events=3)

    opening = scorecard["groups"]["opening_0_60m"]["branches"]["extreme_low_imbalance_reversal"]["horizons"]["30m"]
    assert opening["remove_best_mean_net_pts"] == 2.0
    assert opening["survives_remove_best"] is True

    mid = scorecard["groups"]["mid_60_180m"]["branches"]["extreme_high_imbalance_momentum"]["horizons"]["30m"]
    assert mid["events"] == 2
    assert mid["survives_remove_best"] is False

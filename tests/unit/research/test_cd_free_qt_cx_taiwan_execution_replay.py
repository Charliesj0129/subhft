from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from research.experiments.validations.cd_free_qt_cx_taiwan_v0.execution_replay import (
    Alert,
    Bar,
    BboQuote,
    ReplayConfig,
    contract_for_date,
    first_eligible_quote,
    load_session_bbo_from_hftbt_npz,
    simulate_fixed_risk,
    simulate_structural,
    stage_for_date,
    summarize_trades,
    wilder_atr_by_end,
)
from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    LOCAL_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    _event_dtype,
)

DATE = "2026-04-17"


def _q(ts: int, bid: float, ask: float) -> BboQuote:
    return BboQuote(ts_ns=ts, bid=bid, ask=ask, bid_qty=1.0, ask_qty=1.0)


def _alert(event_id: str, ts: int, direction: int = 1) -> Alert:
    return Alert(
        event_id=event_id,
        lane="baseline_sweep_cisd",
        direction=direction,
        ts_ns=ts,
        date=DATE,
    )


def test_first_eligible_quote_is_strictly_after_confirmation_and_latency():
    quotes = [
        _q(100, 99, 101),
        _q(156, 100, 102),
        _q(157, 101, 103),
        _q(158, 105, 104),
    ]

    quote = first_eligible_quote(quotes, confirmation_ns=100, latency_ns=57)

    assert quote == quotes[2]
    assert first_eligible_quote([_q(157, 105, 104)], confirmation_ns=100, latency_ns=57) is None


def test_wilder_atr_is_causal_and_keyed_by_confirmed_bar_end():
    bars = [Bar(i, 10.0, 12.0, 9.0, close) for i, close in enumerate([10.0, 11.0, 10.0, 12.0, 11.0], start=1)]

    atr = wilder_atr_by_end(bars, period=3)

    assert atr[2] is None
    assert atr[3] == pytest.approx(3.0)
    assert atr[4] == pytest.approx(3.0)
    assert atr[5] == pytest.approx(3.0)


def test_fixed_risk_long_crosses_ask_and_observes_bid_for_stop():
    config = ReplayConfig(latency_ns=57, cutoff_by_date={DATE: 1_000})
    alerts = [_alert("a", 100, 1)]
    quotes = {
        DATE: [
            _q(157, 100, 102),
            _q(200, 99, 101),
            _q(250, 98, 100),
        ]
    }

    result = simulate_fixed_risk(
        alerts,
        quotes,
        atr_by_alert={"a": 4.0},
        stop_atr=0.75,
        target_atr=1.0,
        config=config,
    )

    assert result.rejections == {}
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_px == 102
    assert trade.exit_px == 99
    assert trade.exit_reason == "stop"
    assert trade.gross_points == -3


def test_fixed_risk_short_crosses_bid_and_observes_ask_for_target():
    config = ReplayConfig(latency_ns=57, cutoff_by_date={DATE: 1_000})
    quotes = {DATE: [_q(157, 100, 102), _q(200, 96, 98)]}

    result = simulate_fixed_risk(
        [_alert("a", 100, -1)],
        quotes,
        atr_by_alert={"a": 2.0},
        stop_atr=1.0,
        target_atr=1.0,
        config=config,
    )

    trade = result.trades[0]
    assert trade.entry_px == 100
    assert trade.exit_px == 98
    assert trade.exit_reason == "target"
    assert trade.gross_points == 2


def test_structural_exit_uses_delayed_opposite_cisd_before_force_flat():
    config = ReplayConfig(latency_ns=57, cutoff_by_date={DATE: 1_000})
    quotes = {DATE: [_q(157, 100, 102), _q(500, 104, 106), _q(557, 105, 107)]}
    opposite = [_alert("opposite", 500, -1)]

    result = simulate_structural([_alert("a", 100, 1)], opposite, quotes, config=config)

    trade = result.trades[0]
    assert trade.exit_ts_ns == 557
    assert trade.exit_px == 105
    assert trade.exit_reason == "opposite_cisd"


def test_one_position_rule_skips_alerts_while_existing_trade_is_open():
    config = ReplayConfig(latency_ns=57, cutoff_by_date={DATE: 1_000})
    quotes = {DATE: [_q(157, 100, 102), _q(357, 99, 101), _q(1_000, 103, 105)]}
    alerts = [_alert("a", 100, 1), _alert("b", 200, 1)]

    result = simulate_fixed_risk(
        alerts,
        quotes,
        atr_by_alert={"a": 100.0, "b": 100.0},
        stop_atr=1.0,
        target_atr=2.0,
        config=config,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "force_flat"
    assert result.rejections == {"position_open": 1}


def test_scorecard_deducts_costs_and_reports_concentration_and_force_flat():
    config = ReplayConfig(latency_ns=57, cutoff_by_date={DATE: 1_000})
    quotes = {DATE: [_q(157, 100, 102), _q(1_000, 110, 112)]}
    result = simulate_fixed_risk(
        [_alert("a", 100, 1)],
        quotes,
        atr_by_alert={"a": 100.0},
        stop_atr=1.0,
        target_atr=2.0,
        config=config,
    )

    score = summarize_trades(result.trades, costs=(0.0, 3.0, 6.0))

    assert score["3pt"]["n_trades"] == 1
    assert score["3pt"]["net_mean"] == 5.0
    assert score["3pt"]["force_flat_share"] == 1.0
    assert score["3pt"]["best_day_loo_total"] == 0.0


def test_hftbt_loader_emits_only_real_depth_update_timestamps(tmp_path):
    tz = ZoneInfo("Asia/Taipei")
    base = int(datetime(2026, 4, 17, 9, 0, tzinfo=tz).timestamp() * 1_000_000_000)
    flags = EXCH_EVENT | LOCAL_EVENT
    rows = np.array(
        [
            (DEPTH_CLEAR_EVENT | flags, base, base, 0.0, 0.0, 0, 0, 0.0),
            (DEPTH_EVENT | flags | BUY_EVENT, base, base, 100.0, 2.0, 0, 0, 0.0),
            (DEPTH_EVENT | flags | SELL_EVENT, base, base, 102.0, 3.0, 0, 0, 0.0),
            (TRADE_EVENT | flags | BUY_EVENT, base + 10, base + 10, 102.0, 1.0, 0, 0, 0.0),
            (DEPTH_CLEAR_EVENT | flags, base + 20, base + 20, 0.0, 0.0, 0, 0, 0.0),
            (DEPTH_EVENT | flags | BUY_EVENT, base + 20, base + 20, 101.0, 4.0, 0, 0, 0.0),
            (DEPTH_EVENT | flags | SELL_EVENT, base + 20, base + 20, 103.0, 5.0, 0, 0, 0.0),
        ],
        dtype=_event_dtype(),
    )
    path = tmp_path / "TXFE6_2026-04-17_l2.hftbt.npz"
    np.savez_compressed(path, data=rows)

    quotes = load_session_bbo_from_hftbt_npz(path, date=DATE)

    assert [quote.ts_ns for quote in quotes] == [base, base + 20]
    assert [(quote.bid, quote.ask) for quote in quotes] == [(100.0, 102.0), (101.0, 103.0)]


def test_frozen_front_chain_and_oos_stages_are_not_result_selected():
    assert contract_for_date("2026-05-20") == "TXFE6"
    assert contract_for_date("2026-05-21") == "TXFF6"
    assert stage_for_date("2026-04-16") == "primary_oos"
    assert stage_for_date("2026-05-21") == "confirmation_oos"

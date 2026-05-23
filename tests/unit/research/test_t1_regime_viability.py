from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from research.t1.regime_viability import (
    OpeningRangeConfig,
    coverage_audit_opening_range,
    detect_opening_range_events,
    evaluate_executable_returns,
    extract_bbo_and_trades,
    make_time_bars,
)

HFTBT_DTYPE = np.dtype(
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

BID_DEPTH = np.uint64(0xE0000002)
ASK_DEPTH = np.uint64(0xD0000002)
BID_TRADE = np.uint64(0xE0000001)
ASK_TRADE = np.uint64(0xD0000001)


def ns_at(hour: int, minute: int, second: int = 0) -> int:
    dt = datetime(2026, 3, 3, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def event(ev: np.uint64, ts: int, px: float, qty: float = 1.0) -> tuple:
    return (ev, ts, ts, px, qty, 0, 0, 0.0)


def test_extract_bbo_and_trades_reconstructs_top_of_book_and_trade_prints():
    arr = np.array(
        [
            event(BID_DEPTH, ns_at(0, 45), 100.0, 3.0),
            event(ASK_DEPTH, ns_at(0, 45), 101.0, 4.0),
            event(ASK_TRADE, ns_at(0, 46), 101.0, 2.0),
            event(ASK_DEPTH, ns_at(0, 47), 101.0, 0.0),
            event(BID_DEPTH, ns_at(0, 47), 102.0, 1.0),
            event(ASK_DEPTH, ns_at(0, 47), 103.0, 1.0),
        ],
        dtype=HFTBT_DTYPE,
    )

    bbo, trades = extract_bbo_and_trades(arr)

    assert bbo.ts_ns.tolist() == [ns_at(0, 45), ns_at(0, 47)]
    assert bbo.bid.tolist() == [100.0, 102.0]
    assert bbo.ask.tolist() == [101.0, 103.0]
    assert bbo.mid.tolist() == [100.5, 102.5]
    assert trades.ts_ns.tolist() == [ns_at(0, 46)]
    assert trades.price.tolist() == [101.0]
    assert trades.qty.tolist() == [2.0]


def test_make_time_bars_uses_last_mid_and_quote_high_low():
    bbo, _ = extract_bbo_and_trades(
        np.array(
            [
                event(BID_DEPTH, ns_at(0, 45), 100.0),
                event(ASK_DEPTH, ns_at(0, 45), 101.0),
                event(BID_DEPTH, ns_at(0, 50), 100.0, 0.0),
                event(ASK_DEPTH, ns_at(0, 50), 101.0, 0.0),
                event(BID_DEPTH, ns_at(0, 50), 103.0),
                event(ASK_DEPTH, ns_at(0, 50), 104.0),
                event(BID_DEPTH, ns_at(1, 1), 103.0, 0.0),
                event(ASK_DEPTH, ns_at(1, 1), 104.0, 0.0),
                event(BID_DEPTH, ns_at(1, 1), 99.0),
                event(ASK_DEPTH, ns_at(1, 1), 100.0),
            ],
            dtype=HFTBT_DTYPE,
        )
    )

    bars = make_time_bars(bbo, interval_minutes=15)

    assert len(bars) == 2
    assert bars[0].open == 100.5
    assert bars[0].high == 103.5
    assert bars[0].low == 100.5
    assert bars[0].close == 103.5
    assert bars[1].close == 99.5


def test_detect_opening_range_event_requires_break_vwap_side_and_realized_vol_expansion():
    rows = []
    prev_bid = None
    prev_ask = None
    for minute, bid, ask in [
        (45, 100.0, 101.0),
        (50, 101.0, 102.0),
        (55, 99.0, 100.0),
        (60, 100.0, 101.0),
        (65, 101.0, 102.0),
        (70, 106.0, 107.0),
    ]:
        ts = ns_at(0 if minute < 60 else 1, minute % 60)
        if prev_bid is not None:
            rows.append(event(BID_DEPTH, ts, prev_bid, 0.0))
        if prev_ask is not None:
            rows.append(event(ASK_DEPTH, ts, prev_ask, 0.0))
        rows.append(event(BID_DEPTH, ts, bid))
        rows.append(event(ASK_DEPTH, ts, ask))
        rows.append(event(ASK_TRADE, ts + 1_000_000_000, ask, 2.0))
        prev_bid = bid
        prev_ask = ask
    bbo, trades = extract_bbo_and_trades(np.array(rows, dtype=HFTBT_DTYPE))

    events = detect_opening_range_events(
        bbo,
        trades,
        contract="TXFC6",
        date="2026-03-03",
        config=OpeningRangeConfig(
            session_start_ns=ns_at(0, 45),
            opening_minutes=15,
            confirm_minutes=15,
            min_break_points=3.0,
            min_rv_ratio=1.2,
        ),
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.regime_type == "T1-A_opening_range_expansion"
    assert ev.direction == 1
    assert ev.opening_range_high == 101.5
    assert ev.txf_entry_ref == 106.5


def test_evaluate_executable_returns_uses_ask_entry_for_long_and_bid_path_for_long_exit():
    tmf_bbo, _ = extract_bbo_and_trades(
        np.array(
            [
                event(BID_DEPTH, ns_at(1, 0), 50.0),
                event(ASK_DEPTH, ns_at(1, 0), 51.0),
                event(BID_DEPTH, ns_at(1, 10), 50.0, 0.0),
                event(ASK_DEPTH, ns_at(1, 10), 51.0, 0.0),
                event(BID_DEPTH, ns_at(1, 10), 55.0),
                event(ASK_DEPTH, ns_at(1, 10), 56.0),
                event(BID_DEPTH, ns_at(1, 20), 55.0, 0.0),
                event(ASK_DEPTH, ns_at(1, 20), 56.0, 0.0),
                event(BID_DEPTH, ns_at(1, 20), 48.0),
                event(ASK_DEPTH, ns_at(1, 20), 49.0),
                event(BID_DEPTH, ns_at(1, 30), 48.0, 0.0),
                event(ASK_DEPTH, ns_at(1, 30), 49.0, 0.0),
                event(BID_DEPTH, ns_at(1, 30), 53.0),
                event(ASK_DEPTH, ns_at(1, 30), 54.0),
            ],
            dtype=HFTBT_DTYPE,
        )
    )

    row = evaluate_executable_returns(
        tmf_bbo,
        trigger_time_ns=ns_at(1, 0),
        direction=1,
        horizons_minutes=(15, 30),
    )

    assert row["tmf_executable_entry"] == 51.0
    assert row["mfe_15m"] == 4.0
    assert row["mae_30m"] == -3.0
    assert row["return_30m"] == 2.0
    assert row["time_to_mfe"] == 10 * 60
    assert row["time_to_mae"] == 20 * 60


def test_audit_opening_range_pair_emits_scorecard_net_30m_alias(tmp_path):
    from research.t1.regime_viability import audit_opening_range_pair

    txf_events = []
    tmf_events = []
    prev_bid = prev_ask = None
    for minute, bid, ask in [
        (45, 100.0, 101.0),
        (50, 101.0, 102.0),
        (55, 99.0, 100.0),
        (60, 100.0, 101.0),
        (65, 101.0, 102.0),
        (70, 106.0, 107.0),
        (80, 108.0, 109.0),
        (95, 109.0, 110.0),
    ]:
        ts = ns_at(0 if minute < 60 else 1, minute % 60)
        if prev_bid is not None:
            txf_events.append(event(BID_DEPTH, ts, prev_bid, 0.0))
            txf_events.append(event(ASK_DEPTH, ts, prev_ask, 0.0))
        txf_events.append(event(BID_DEPTH, ts, bid))
        txf_events.append(event(ASK_DEPTH, ts, ask))
        txf_events.append(event(ASK_TRADE, ts + 1_000_000_000, ask, 2.0))
        prev_bid, prev_ask = bid, ask

        tmf_events.append(event(BID_DEPTH, ts, bid))
        tmf_events.append(event(ASK_DEPTH, ts, ask))

    txf_path = tmp_path / "TXFC6_2026-03-03_l2.hftbt.npz"
    tmf_path = tmp_path / "TMFC6_2026-03-03_l2.hftbt.npz"
    np.savez(txf_path, data=np.array(txf_events, dtype=HFTBT_DTYPE))
    np.savez(tmf_path, data=np.array(tmf_events, dtype=HFTBT_DTYPE))

    rows = audit_opening_range_pair(
        txf_path=txf_path,
        tmf_path=tmf_path,
        session_tz_offset_hours=8,
        opening_minutes=15,
        confirm_minutes=15,
        min_break_points=3.0,
        min_rv_ratio=1.2,
    )

    assert len(rows) == 1
    assert rows[0]["net_30m_pts"] == rows[0]["return_30m"]


def test_coverage_audit_reports_pure_break_even_when_v0_filters_it_out():
    rows = []
    prev_bid = None
    prev_ask = None
    for minute, bid, ask in [
        (45, 100.0, 101.0),
        (50, 101.0, 102.0),
        (55, 99.0, 100.0),
        (60, 103.0, 104.0),
        (65, 104.0, 105.0),
        (70, 105.0, 106.0),
    ]:
        ts = ns_at(0 if minute < 60 else 1, minute % 60)
        if prev_bid is not None:
            rows.append(event(BID_DEPTH, ts, prev_bid, 0.0))
        if prev_ask is not None:
            rows.append(event(ASK_DEPTH, ts, prev_ask, 0.0))
        rows.append(event(BID_DEPTH, ts, bid))
        rows.append(event(ASK_DEPTH, ts, ask))
        rows.append(event(ASK_TRADE, ts + 1_000_000_000, ask, 1.0))
        prev_bid = bid
        prev_ask = ask
    bbo, trades = extract_bbo_and_trades(np.array(rows, dtype=HFTBT_DTYPE))

    row = coverage_audit_opening_range(
        bbo,
        trades,
        contract="TXFC6",
        trading_day="2026-03-03",
        pair_id="TXFC6->TMFC6",
        config=OpeningRangeConfig(
            session_start_ns=ns_at(0, 45),
            opening_minutes=15,
            confirm_minutes=15,
            min_break_points=8.0,
            min_rv_ratio=9.0,
        ),
        persistence_minutes=5,
    )

    assert row["contract"] == "TXFC6"
    assert row["trading_day"] == "2026-03-03"
    assert row["pair_id"] == "TXFC6->TMFC6"
    assert row["or_high"] == 101.5
    assert row["or_low"] == 99.5
    assert row["or_width"] == 2.0
    assert row["post_or_high"] == 105.5
    assert row["post_or_low"] == 103.5
    assert row["max_upside_break_pts"] == 4.0
    assert row["max_downside_break_pts"] == 0.0
    assert row["break_side"] == "up"
    assert row["break_magnitude_pts"] == 2.0
    assert row["break_magnitude_vs_or_width"] == 1.0
    assert row["vwap_side_at_break"] == "above"
    assert row["reverted_to_or"] is False
    assert row["time_above_or_high"] == 15 * 60
    assert row["time_below_or_low"] == 0
    assert row["event_selected_by_v0"] is False


def test_coverage_v0_flag_uses_detector_trigger_not_first_touch_magnitude():
    rows = []
    prev_bid = None
    prev_ask = None
    for minute, bid, ask in [
        (45, 100.0, 101.0),
        (50, 101.0, 102.0),
        (55, 99.0, 100.0),
        (60, 102.0, 103.0),  # first OR touch: only +1.0 above 101.5
        (65, 110.0, 111.0),  # detector trigger: +9.0 above 101.5
        (70, 112.0, 113.0),
    ]:
        ts = ns_at(0 if minute < 60 else 1, minute % 60)
        if prev_bid is not None:
            rows.append(event(BID_DEPTH, ts, prev_bid, 0.0))
        if prev_ask is not None:
            rows.append(event(ASK_DEPTH, ts, prev_ask, 0.0))
        rows.append(event(BID_DEPTH, ts, bid))
        rows.append(event(ASK_DEPTH, ts, ask))
        rows.append(event(ASK_TRADE, ts + 1_000_000_000, ask, 1.0))
        prev_bid = bid
        prev_ask = ask
    bbo, trades = extract_bbo_and_trades(np.array(rows, dtype=HFTBT_DTYPE))
    config = OpeningRangeConfig(
        session_start_ns=ns_at(0, 45),
        opening_minutes=15,
        confirm_minutes=15,
        min_break_points=8.0,
        min_rv_ratio=1.1,
    )

    events = detect_opening_range_events(
        bbo,
        trades,
        contract="TXFC6",
        date="2026-03-03",
        config=config,
    )
    row = coverage_audit_opening_range(
        bbo,
        trades,
        contract="TXFC6",
        trading_day="2026-03-03",
        pair_id="TXFC6->TMFC6",
        config=config,
    )

    assert len(events) == 1
    assert row["break_magnitude_pts"] == 1.0
    assert row["event_selected_by_v0"] is True


def test_coverage_audit_preserves_opening_range_when_post_window_missing():
    bbo, trades = extract_bbo_and_trades(
        np.array(
            [
                event(BID_DEPTH, ns_at(0, 45), 100.0),
                event(ASK_DEPTH, ns_at(0, 45), 101.0),
                event(BID_DEPTH, ns_at(0, 50), 102.0),
                event(ASK_DEPTH, ns_at(0, 50), 103.0),
            ],
            dtype=HFTBT_DTYPE,
        )
    )

    row = coverage_audit_opening_range(
        bbo,
        trades,
        contract="TXFC6",
        trading_day="2026-03-03",
        pair_id="TXFC6->TMFC6",
        config=OpeningRangeConfig(
            session_start_ns=ns_at(0, 45),
            opening_minutes=15,
            confirm_minutes=15,
        ),
    )

    assert row["coverage_status"] == "missing_post"
    assert row["or_high"] == 102.5
    assert row["or_low"] == 100.5
    assert row["post_or_high"] is None
    assert row["break_side"] == "none"

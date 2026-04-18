from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    EV_TYPE_MASK,
    EXCH_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    BacktestDataSource,
    ChDataSource,
    DataValidationError,
    _event_dtype,
    assemble_day_events,
    build_bidask_events,
    build_bidask_events_diff,
    build_tick_event,
    validate_events,
)

# ---------------------------------------------------------------------------
# B1 — Basic infrastructure
# ---------------------------------------------------------------------------


def test_data_validation_error_is_exception():
    assert issubclass(DataValidationError, Exception)


def test_ch_data_source_implements_protocol():
    src = ChDataSource(ch_host="localhost", ch_port=8123, price_scale=1_000_000)
    assert isinstance(src, BacktestDataSource)


def test_ch_data_source_default_config():
    src = ChDataSource()
    assert src.price_scale == 1_000_000
    assert src.ch_host == "localhost"
    assert src.ch_port == 8123
    assert src.ch_user == "default"
    # password comes from env (empty if CLICKHOUSE_PASSWORD not set)
    assert isinstance(src.ch_password, str)


def test_ch_data_source_explicit_credentials():
    src = ChDataSource(ch_user="admin", ch_password="secret")
    assert src.ch_user == "admin"
    assert src.ch_password == "secret"


# ---------------------------------------------------------------------------
# B2 — build_bidask_events / build_tick_event
# ---------------------------------------------------------------------------


def test_build_bidask_events_emits_clear_first():
    events = build_bidask_events(
        exch_ts=1_700_000_000_000_000_000,
        local_ts=1_700_000_000_001_000_000,
        bid_prices=[17000_000_000, 16999_000_000],
        bid_volumes=[5, 10],
        ask_prices=[17001_000_000, 17002_000_000],
        ask_volumes=[3, 7],
        price_scale=1_000_000,
    )
    # First event should be DEPTH_CLEAR
    assert events[0]["ev"] & DEPTH_CLEAR_EVENT
    # Plus 2 bid events + 2 ask events = 5 total
    assert len(events) == 5


def test_build_bidask_events_bid_side_flagged():
    events = build_bidask_events(
        exch_ts=1_700_000_000_000_000_000,
        local_ts=1_700_000_000_001_000_000,
        bid_prices=[17000_000_000],
        bid_volumes=[5],
        ask_prices=[17001_000_000],
        ask_volumes=[3],
        price_scale=1_000_000,
    )
    # events[0] = clear, events[1] = bid, events[2] = ask
    assert events[1]["ev"] & DEPTH_EVENT
    assert events[1]["ev"] & BUY_EVENT
    assert events[2]["ev"] & DEPTH_EVENT
    assert events[2]["ev"] & SELL_EVENT


def test_build_bidask_events_prices_descaled():
    events = build_bidask_events(
        exch_ts=1,
        local_ts=2,
        bid_prices=[17000_000_000],
        bid_volumes=[5],
        ask_prices=[17001_000_000],
        ask_volumes=[3],
        price_scale=1_000_000,
    )
    assert events[1]["px"] == pytest.approx(17000.0)
    assert events[2]["px"] == pytest.approx(17001.0)


def test_build_bidask_events_skips_zero_volume():
    events = build_bidask_events(
        exch_ts=1,
        local_ts=2,
        bid_prices=[17000_000_000, 16999_000_000],
        bid_volumes=[5, 0],  # second level zero
        ask_prices=[17001_000_000],
        ask_volumes=[3],
        price_scale=1_000_000,
    )
    # 1 clear + 1 bid + 1 ask = 3 (not 4)
    assert len(events) == 3


def test_build_tick_event_buy():
    event = build_tick_event(
        exch_ts=1_700_000_000_000_000_000,
        local_ts=1_700_000_000_001_000_000,
        price=17000_500_000, volume=2, side="Buy",
        price_scale=1_000_000,
    )
    assert event["ev"] & TRADE_EVENT
    assert event["ev"] & BUY_EVENT
    assert event["px"] == pytest.approx(17000.5)
    assert event["qty"] == 2.0


def test_build_tick_event_sell():
    event = build_tick_event(
        exch_ts=1, local_ts=2,
        price=17000_000_000, volume=1, side="Sell",
        price_scale=1_000_000,
    )
    assert event["ev"] & TRADE_EVENT
    assert event["ev"] & SELL_EVENT


# ---------------------------------------------------------------------------
# B3 — assemble_day_events
# ---------------------------------------------------------------------------


def test_assemble_day_events_sorts_by_exch_ts():
    """Rows out-of-order by exch_ts must be re-sorted; uses trade_direction column."""
    df = pd.DataFrame({
        "exch_ts": [300, 100, 200],
        "local_ts": [301, 101, 201],
        "event_type": ["Tick", "BidAsk", "Tick"],
        "price": [17000_500_000, 0, 17001_000_000],
        "volume": [1, 0, 2],
        "trade_direction": [1, 0, -1],
        "bid_prices": [None, [17000_000_000, 16999_000_000], None],
        "bid_volumes": [None, [5, 10], None],
        "ask_prices": [None, [17001_000_000, 17002_000_000], None],
        "ask_volumes": [None, [3, 7], None],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    # Timestamps must be monotonically non-decreasing
    assert np.all(events["exch_ts"][1:] >= events["exch_ts"][:-1])


def test_assemble_day_events_legacy_side_column():
    """Legacy side='Buy'/'Sell' column still works when trade_direction absent."""
    df = pd.DataFrame({
        "exch_ts": [100, 200],
        "local_ts": [101, 201],
        "event_type": ["BidAsk", "Tick"],
        "price": [0, 17000_500_000],
        "volume": [0, 1],
        "side": [None, "Buy"],
        "bid_prices": [[17000_000_000], None],
        "bid_volumes": [[5], None],
        "ask_prices": [[17001_000_000], None],
        "ask_volumes": [[3], None],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    trade_events = events[(events["ev"] & EV_TYPE_MASK) == TRADE_EVENT]
    assert len(trade_events) == 1
    assert trade_events[0]["ev"] & BUY_EVENT


def test_assemble_day_events_skips_trade_direction_zero():
    """trade_direction==0 Tick rows must be dropped (no executable side)."""
    df = pd.DataFrame({
        "exch_ts": [100, 200, 300],
        "local_ts": [101, 201, 301],
        "event_type": ["BidAsk", "Tick", "Tick"],
        "price": [0, 17000_000_000, 17001_000_000],
        "volume": [0, 1, 2],
        "trade_direction": [0, 0, 1],  # second Tick has direction=0 → skip
        "bid_prices": [[17000_000_000], None, None],
        "bid_volumes": [[5], None, None],
        "ask_prices": [[17001_000_000], None, None],
        "ask_volumes": [[3], None, None],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    trade_events = events[(events["ev"] & EV_TYPE_MASK) == TRADE_EVENT]
    # Only 1 trade event (trade_direction=1 Tick); direction=0 is skipped
    assert len(trade_events) == 1
    assert trade_events[0]["ev"] & BUY_EVENT


# ---------------------------------------------------------------------------
# B4 — validate_events
# ---------------------------------------------------------------------------


def _make_events(items):
    return np.array(items, dtype=_event_dtype())


def test_validate_events_accepts_valid_data():
    events = _make_events([
        (DEPTH_CLEAR_EVENT | EXCH_EVENT, 1, 2, 0, 0, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 2, 3, 17000.0, 5, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT, 2, 3, 17001.0, 3, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 3, 4, 17000.5, 1, 0, 0, 0.0),
    ])
    validate_events(events, instrument="TMFD6")
    # No exception raised; assert postcondition: array is unchanged
    assert len(events) == 4


def test_validate_events_no_depth_raises():
    events = _make_events([
        (TRADE_EVENT | EXCH_EVENT, 1, 2, 17000.0, 1, 0, 0, 0.0),
    ])
    with pytest.raises(DataValidationError, match="no depth events"):
        validate_events(events, instrument="TMFD6")


def test_validate_events_no_trade_raises():
    events = _make_events([
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 1, 2, 17000.0, 5, 0, 0, 0.0),
    ])
    with pytest.raises(DataValidationError, match="no trade events"):
        validate_events(events, instrument="TMFD6")


def test_validate_events_non_monotonic_raises():
    events = _make_events([
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 10, 2, 17000.0, 5, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 5, 3, 17000.0, 1, 0, 0, 0.0),
    ])
    with pytest.raises(DataValidationError, match="monotonic"):
        validate_events(events, instrument="TMFD6")


def test_validate_events_negative_price_raises():
    events = _make_events([
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 1, 2, -17000.0, 5, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT, 2, 3, 17000.0, 1, 0, 0, 0.0),
    ])
    with pytest.raises(DataValidationError, match="negative.*price"):
        validate_events(events, instrument="TMFD6")


def test_validate_events_empty_raises():
    empty = _make_events([])
    with pytest.raises(DataValidationError, match="empty"):
        validate_events(empty, instrument="TMFD6")


def test_validate_events_inverted_book_raises():
    """Book with best_ask < best_bid must raise; locked book (==) is allowed."""
    events = _make_events([
        (DEPTH_CLEAR_EVENT | EXCH_EVENT, 1, 2, 0.0, 0.0, 0, 0, 0.0),
        # bid at 17005, ask at 17000 — strictly inverted book
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 2, 3, 17005.0, 5, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT, 2, 3, 17000.0, 3, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 3, 4, 17002.0, 1, 0, 0, 0.0),
    ])
    with pytest.raises(DataValidationError, match="inverted book"):
        validate_events(events, instrument="TMFD6")


def test_validate_events_locked_book_allowed():
    """Locked book (bid == ask) is a transient matching state, not inverted."""
    events = _make_events([
        (DEPTH_CLEAR_EVENT | EXCH_EVENT, 1, 2, 0.0, 0.0, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 2, 3, 17000.0, 5, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT, 2, 3, 17000.0, 3, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 3, 4, 17000.0, 1, 0, 0, 0.0),
    ])
    validate_events(events, instrument="TMFD6")
    assert len(events) == 4


def test_validate_events_valid_book_not_inverted():
    """Correctly ordered book (ask > bid) must not raise."""
    events = _make_events([
        (DEPTH_CLEAR_EVENT | EXCH_EVENT, 1, 2, 0.0, 0.0, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 2, 3, 17000.0, 5, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT, 2, 3, 17001.0, 3, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 3, 4, 17000.5, 1, 0, 0, 0.0),
    ])
    validate_events(events, instrument="TMFD6")
    # No exception raised; assert postcondition
    assert len(events) == 4


# ---------------------------------------------------------------------------
# B4 — load_day (mocked ClickHouse)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B5 — Incremental depth diff (queue-model-preserving)
# ---------------------------------------------------------------------------


def test_build_bidask_events_diff_unchanged_emits_nothing():
    """When snapshot matches previous state, diff must emit zero events."""
    prev_bids = {17000_000_000: 5, 16999_000_000: 10}
    prev_asks = {17001_000_000: 3, 17002_000_000: 7}
    events = build_bidask_events_diff(
        exch_ts=100, local_ts=101,
        prev_bid_map=prev_bids, prev_ask_map=prev_asks,
        bid_prices=[17000_000_000, 16999_000_000],
        bid_volumes=[5, 10],
        ask_prices=[17001_000_000, 17002_000_000],
        ask_volumes=[3, 7],
        price_scale=1_000_000,
    )
    assert len(events) == 0


def test_build_bidask_events_diff_removes_stale_level():
    """When previous level disappears, diff must emit qty=0 for that level."""
    prev_bids = {17000_000_000: 5, 16999_000_000: 10}
    prev_asks = {17001_000_000: 3}
    events = build_bidask_events_diff(
        exch_ts=100, local_ts=101,
        prev_bid_map=prev_bids, prev_ask_map=prev_asks,
        bid_prices=[17000_000_000], bid_volumes=[5],
        ask_prices=[17001_000_000], ask_volumes=[3],
        price_scale=1_000_000,
    )
    removals = [e for e in events if e["qty"] == 0.0]
    assert len(removals) == 1
    assert removals[0]["ev"] & BUY_EVENT
    assert removals[0]["px"] == pytest.approx(16999.0)


def test_build_bidask_events_diff_changed_volume():
    """When volume at existing level changes, diff emits DEPTH_EVENT with new qty."""
    prev_bids = {17000_000_000: 5}
    prev_asks = {17001_000_000: 3}
    events = build_bidask_events_diff(
        exch_ts=100, local_ts=101,
        prev_bid_map=prev_bids, prev_ask_map=prev_asks,
        bid_prices=[17000_000_000], bid_volumes=[8],
        ask_prices=[17001_000_000], ask_volumes=[3],
        price_scale=1_000_000,
    )
    assert len(events) == 1
    assert events[0]["ev"] & BUY_EVENT
    assert events[0]["qty"] == 8.0


def test_build_bidask_events_diff_mutates_prev_maps():
    """Diff must update prev maps in place to reflect new snapshot."""
    prev_bids = {17000_000_000: 5}
    prev_asks = {17001_000_000: 3}
    build_bidask_events_diff(
        exch_ts=100, local_ts=101,
        prev_bid_map=prev_bids, prev_ask_map=prev_asks,
        bid_prices=[17000_500_000], bid_volumes=[4],
        ask_prices=[17002_000_000], ask_volumes=[6],
        price_scale=1_000_000,
    )
    assert prev_bids == {17000_500_000: 4}
    assert prev_asks == {17002_000_000: 6}


def test_assemble_day_events_only_first_snapshot_clears():
    """Multi-snapshot day emits exactly one DEPTH_CLEAR (at the start)."""
    df = pd.DataFrame({
        "exch_ts": [100, 200, 300],
        "local_ts": [101, 201, 301],
        "event_type": ["BidAsk", "BidAsk", "BidAsk"],
        "price": [0, 0, 0],
        "volume": [0, 0, 0],
        "trade_direction": [0, 0, 0],
        "bid_prices": [
            [17000_000_000], [17000_000_000], [17000_500_000]
        ],
        "bid_volumes": [[5], [6], [4]],
        "ask_prices": [
            [17001_000_000], [17001_000_000], [17001_500_000]
        ],
        "ask_volumes": [[3], [3], [5]],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    ev_types = events["ev"] & EV_TYPE_MASK
    n_clear = int(np.sum(ev_types == DEPTH_CLEAR_EVENT))
    assert n_clear == 1


def test_assemble_day_events_diff_chain_preserves_queue_signal():
    """Second identical snapshot emits no events; third emits only the delta."""
    df = pd.DataFrame({
        "exch_ts": [100, 200, 300],
        "local_ts": [101, 201, 301],
        "event_type": ["BidAsk", "BidAsk", "BidAsk"],
        "price": [0, 0, 0],
        "volume": [0, 0, 0],
        "trade_direction": [0, 0, 0],
        "bid_prices": [[17000_000_000], [17000_000_000], [17000_000_000]],
        "bid_volumes": [[5], [5], [7]],  # volume changed only at t=300
        "ask_prices": [[17001_000_000], [17001_000_000], [17001_000_000]],
        "ask_volumes": [[3], [3], [3]],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    # t=100: 1 clear + 1 bid + 1 ask = 3; t=200: 0; t=300: 1 bid diff = 1. Total 4.
    assert len(events) == 4
    events_at_300 = events[events["exch_ts"] == 300]
    assert len(events_at_300) == 1
    assert events_at_300[0]["qty"] == 7.0


def test_load_day_empty_result_raises():
    """When CH returns no rows, load_day raises DataValidationError."""
    with patch("clickhouse_connect.get_client") as mock_client:
        client = MagicMock()
        client.query_df.return_value = pd.DataFrame()
        mock_client.return_value = client

        src = ChDataSource()
        with pytest.raises(DataValidationError, match="no rows"):
            src.load_day("TMFD6", "2026-03-19")

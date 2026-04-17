from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    BacktestDataSource,
    ChDataSource,
    DataValidationError,
    _event_dtype,
    assemble_day_events,
    build_bidask_events,
    build_tick_event,
    validate_events,
)

# ---------------------------------------------------------------------------
# B1 — Basic infrastructure
# ---------------------------------------------------------------------------


def test_data_validation_error_is_exception():
    assert issubclass(DataValidationError, Exception)


def test_ch_data_source_implements_protocol():
    src = ChDataSource(ch_host="localhost", ch_port=9000, price_scale=1_000_000)
    assert isinstance(src, BacktestDataSource)


def test_ch_data_source_default_config():
    src = ChDataSource()
    assert src.price_scale == 1_000_000
    assert src.ch_host == "localhost"
    assert src.ch_port == 9000


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
    df = pd.DataFrame({
        "exch_ts": [300, 100, 200],
        "local_ts": [301, 101, 201],
        "event_type": ["Tick", "BidAsk", "Tick"],
        "price": [17000_500_000, 0, 17001_000_000],
        "volume": [1, 0, 2],
        "side": ["Buy", None, "Sell"],
        "bid_prices": [None, [17000_000_000, 16999_000_000], None],
        "bid_volumes": [None, [5, 10], None],
        "ask_prices": [None, [17001_000_000, 17002_000_000], None],
        "ask_volumes": [None, [3, 7], None],
    })
    events = assemble_day_events(df, price_scale=1_000_000)
    # Timestamps must be monotonically non-decreasing
    assert np.all(events["exch_ts"][1:] >= events["exch_ts"][:-1])


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


# ---------------------------------------------------------------------------
# B4 — load_day (mocked ClickHouse)
# ---------------------------------------------------------------------------


def test_load_day_empty_result_raises():
    """When CH returns no rows, load_day raises DataValidationError."""
    with patch("clickhouse_connect.get_client") as mock_client:
        client = MagicMock()
        client.query_df.return_value = pd.DataFrame()
        mock_client.return_value = client

        src = ChDataSource()
        with pytest.raises(DataValidationError, match="no rows"):
            src.load_day("TMFD6", "2026-03-19")

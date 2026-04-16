"""Coverage tests for hft_platform.backtest.adapter — missing line ranges.

Targets: HFTBACKTEST_AVAILABLE=False, _record_fill buffer growth,
_record_rejection, _reset_equity_buffers, _maybe_record_equity_point,
fill_stats, _wait_for_next_feed, _build_l1_bidask_event, execute_intent,
_intent_factory, _scale_price, _sync_positions, _read_balance, _fill_log,
_equity properties, _make_feature_lookup, and various queue/exchange models.

Note: HftBacktestAdapter requires the hftbacktest library which may not be
available in unit tests. We test individual methods and isolated logic.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# HFTBACKTEST_AVAILABLE=False (lines 27-28)
# ---------------------------------------------------------------------------


def test_hftbacktest_not_available_raises():
    """When hftbacktest is not installed, HftBacktestAdapter raises ImportError."""
    with patch("hft_platform.backtest.adapter.HFTBACKTEST_AVAILABLE", False):
        from hft_platform.backtest.adapter import HftBacktestAdapter

        with pytest.raises(ImportError, match="hftbacktest not installed"):
            HftBacktestAdapter(
                strategy=MagicMock(),
                asset_symbol="TEST",
                data_path="fake.npz",
            )


# ---------------------------------------------------------------------------
# Adapter with mocked hftbacktest (build a partial mock adapter)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_adapter():
    """Create an adapter-like object with key attributes for method testing."""
    from hft_platform.backtest.adapter import _FILL_CAPACITY, _EQUITY_CAPACITY

    adapter = types.SimpleNamespace()
    adapter.symbol = "TXFD6"
    adapter.price_scale = 10_000
    adapter.positions = {"TXFD6": 0}
    adapter._prev_position = 0
    adapter._total_buy_fills = 0
    adapter._total_sell_fills = 0

    # Fill buffers
    adapter._fill_ts_ns = np.zeros(_FILL_CAPACITY, dtype=np.int64)
    adapter._fill_delta = np.zeros(_FILL_CAPACITY, dtype=np.int32)
    adapter._fill_position_after = np.zeros(_FILL_CAPACITY, dtype=np.int32)
    adapter._fill_mid_price_x2 = np.zeros(_FILL_CAPACITY, dtype=np.int64)
    adapter._fill_count = 0

    # Equity buffers
    adapter.equity_sample_ns = 1_000_000
    adapter._next_equity_sample_ns = 0
    adapter._last_known_balance = 1_000_000.0
    adapter._equity_ts_buf = np.zeros(_EQUITY_CAPACITY, dtype=np.int64)
    adapter._equity_val_buf = np.zeros(_EQUITY_CAPACITY, dtype=np.float64)
    adapter._equity_count = 0

    # Reject buffers
    adapter._reject_ts_ns = np.zeros(256, dtype=np.int64)
    adapter._reject_reasons = []
    adapter._reject_count = 0

    adapter._intent_seq = 0
    adapter._hbt_seq = 0

    return adapter


# ---------------------------------------------------------------------------
# _record_fill: basic and buffer growth (lines 254, 273, 275-282)
# ---------------------------------------------------------------------------


def test_record_fill_basic(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    HftBacktestAdapter._record_fill(mock_adapter, ts_ns=1000, delta=1, position_after=1, mid_price_x2=200000)
    assert mock_adapter._fill_count == 1
    assert mock_adapter._fill_ts_ns[0] == 1000
    assert mock_adapter._fill_delta[0] == 1


def test_record_fill_buffer_growth(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    # Fill to capacity
    cap = mock_adapter._fill_ts_ns.size
    mock_adapter._fill_count = cap
    HftBacktestAdapter._record_fill(mock_adapter, ts_ns=9999, delta=1, position_after=1, mid_price_x2=100)
    assert mock_adapter._fill_ts_ns.size == cap * 2
    assert mock_adapter._fill_count == cap + 1


# ---------------------------------------------------------------------------
# _record_rejection (lines 290, 292)
# ---------------------------------------------------------------------------


def test_record_rejection(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    HftBacktestAdapter._record_rejection(mock_adapter, intent=MagicMock(), reason="max_position")
    assert mock_adapter._reject_count == 1
    assert mock_adapter._reject_reasons[0] == "max_position"


def test_record_rejection_buffer_growth(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._reject_count = 256  # at capacity
    HftBacktestAdapter._record_rejection(mock_adapter, intent=MagicMock(), reason="overflow")
    assert mock_adapter._reject_count == 257
    assert len(mock_adapter._reject_ts_ns) == 512


# ---------------------------------------------------------------------------
# _reset_equity_buffers (line 322-323)
# ---------------------------------------------------------------------------


def test_reset_equity_buffers(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._equity_count = 50
    mock_adapter._next_equity_sample_ns = 999
    HftBacktestAdapter._reset_equity_buffers(mock_adapter)
    assert mock_adapter._equity_count == 0
    assert mock_adapter._next_equity_sample_ns == 0


# ---------------------------------------------------------------------------
# _maybe_record_equity_point (lines 345-349, 358-359, 362, 364-368, 370)
# ---------------------------------------------------------------------------


def test_maybe_record_equity_point_records(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter.hbt = MagicMock()
    mock_adapter._read_balance = lambda asset_id: 1_000_000.0

    # Use bound method via class
    HftBacktestAdapter._maybe_record_equity_point(
        mock_adapter, ts_ns=2_000_000, best_bid=100000, best_ask=100200
    )
    assert mock_adapter._equity_count == 1
    assert mock_adapter._equity_ts_buf[0] == 2_000_000


def test_maybe_record_equity_point_skips_before_next(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._next_equity_sample_ns = 10_000_000
    mock_adapter._read_balance = lambda asset_id: 1_000_000.0

    HftBacktestAdapter._maybe_record_equity_point(
        mock_adapter, ts_ns=5_000_000, best_bid=100000, best_ask=100200
    )
    assert mock_adapter._equity_count == 0


def test_maybe_record_equity_point_disabled(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter.equity_sample_ns = 0
    HftBacktestAdapter._maybe_record_equity_point(
        mock_adapter, ts_ns=2_000_000, best_bid=100000, best_ask=100200
    )
    assert mock_adapter._equity_count == 0


def test_maybe_record_equity_point_buffer_growth(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    cap = mock_adapter._equity_ts_buf.size
    mock_adapter._equity_count = cap
    mock_adapter._read_balance = lambda asset_id: 1_000_000.0

    HftBacktestAdapter._maybe_record_equity_point(
        mock_adapter, ts_ns=2_000_000, best_bid=100000, best_ask=100200
    )
    assert mock_adapter._equity_ts_buf.size == cap * 2


# ---------------------------------------------------------------------------
# fill_stats (lines 376, 383-388)
# ---------------------------------------------------------------------------


def test_fill_stats_empty(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    stats = HftBacktestAdapter.fill_stats.fget(mock_adapter)
    assert stats["total_fills"] == 0
    assert stats["n_fill_events"] == 0
    assert stats["adverse_selection_mean"] == 0.0


def test_fill_stats_with_fills(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._total_buy_fills = 5
    mock_adapter._total_sell_fills = 3
    mock_adapter._fill_count = 3
    mock_adapter._fill_ts_ns[0] = 1000
    mock_adapter._fill_ts_ns[1] = 2000
    mock_adapter._fill_ts_ns[2] = 3000
    mock_adapter._fill_delta[0] = 1
    mock_adapter._fill_delta[1] = -1
    mock_adapter._fill_mid_price_x2[0] = 200000
    mock_adapter._fill_mid_price_x2[1] = 200100
    mock_adapter._fill_mid_price_x2[2] = 200200

    stats = HftBacktestAdapter.fill_stats.fget(mock_adapter)
    assert stats["total_fills"] == 8
    assert stats["n_fill_events"] == 3
    assert "adverse_selection_mean" in stats


# ---------------------------------------------------------------------------
# equity properties (lines 415-418)
# ---------------------------------------------------------------------------


def test_equity_timestamps_and_values(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._equity_count = 2
    mock_adapter._equity_ts_buf[0] = 100
    mock_adapter._equity_ts_buf[1] = 200
    mock_adapter._equity_val_buf[0] = 1000.0
    mock_adapter._equity_val_buf[1] = 1001.0

    ts = HftBacktestAdapter.equity_timestamps_ns.fget(mock_adapter)
    vals = HftBacktestAdapter.equity_values.fget(mock_adapter)
    assert len(ts) == 2
    assert len(vals) == 2


# ---------------------------------------------------------------------------
# _fill_log property (lines 435, 440-441)
# ---------------------------------------------------------------------------


def test_fill_log_property(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._fill_count = 1
    mock_adapter._fill_ts_ns[0] = 1000
    mock_adapter._fill_delta[0] = 1
    mock_adapter._fill_position_after[0] = 1
    mock_adapter._fill_mid_price_x2[0] = 200000

    log = HftBacktestAdapter._fill_log.fget(mock_adapter)
    assert len(log) == 1
    assert log[0]["ts_ns"] == 1000
    assert log[0]["mid_price"] == 100000.0
    assert log[0]["mid_price_x2"] == 200000


# ---------------------------------------------------------------------------
# _intent_factory (line 136)
# ---------------------------------------------------------------------------


def test_intent_factory(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter
    from hft_platform.contracts.strategy import TIF, IntentType, Side

    intent = HftBacktestAdapter._intent_factory(
        mock_adapter,
        strategy_id="s1",
        symbol="TXFD6",
        side=Side.BUY,
        price=100000,
        qty=1,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )
    assert intent.strategy_id == "s1"
    assert intent.side == Side.BUY
    assert mock_adapter._intent_seq == 1


# ---------------------------------------------------------------------------
# _sync_positions error handling (line 202, 204, 206)
# ---------------------------------------------------------------------------


def test_sync_positions_success(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.position.return_value = 5

    HftBacktestAdapter._sync_positions(mock_adapter)
    assert mock_adapter.positions["TXFD6"] == 5


def test_sync_positions_error(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.position.side_effect = RuntimeError("sync error")

    HftBacktestAdapter._sync_positions(mock_adapter)
    assert mock_adapter.positions["TXFD6"] == 0  # unchanged


# ---------------------------------------------------------------------------
# _read_balance fallback (lines 217, 221, 223, 232-233, 237)
# ---------------------------------------------------------------------------


def test_read_balance_first_method(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    hbt_mock = MagicMock()
    hbt_mock.balance.return_value = 500_000.0
    mock_adapter.hbt = hbt_mock

    result = HftBacktestAdapter._read_balance(mock_adapter, 0)
    assert result == 500_000.0


def test_read_balance_fallback_to_cash(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    hbt_mock = MagicMock()
    hbt_mock.balance = None  # not callable
    hbt_mock.cash.return_value = 750_000.0
    mock_adapter.hbt = hbt_mock

    result = HftBacktestAdapter._read_balance(mock_adapter, 0)
    assert result == 750_000.0


def test_read_balance_type_error_fallback(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    hbt_mock = MagicMock()
    hbt_mock.balance.side_effect = TypeError("bad arg")
    hbt_mock.cash.side_effect = TypeError("bad arg")
    hbt_mock.asset_balance = None
    mock_adapter.hbt = hbt_mock

    result = HftBacktestAdapter._read_balance(mock_adapter, 0)
    # Falls back to _last_known_balance
    assert result == 1_000_000.0


def test_read_balance_no_arg_fallback(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    hbt_mock = MagicMock()
    # balance(asset_id) raises TypeError, but balance() returns 300K
    hbt_mock.balance.side_effect = TypeError("bad arg")
    hbt_mock.cash.return_value = 300_000.0
    mock_adapter.hbt = hbt_mock

    result = HftBacktestAdapter._read_balance(mock_adapter, 0)
    assert result == 300_000.0


# ---------------------------------------------------------------------------
# _wait_for_next_feed status modes (line 160-162, 170)
# ---------------------------------------------------------------------------


def test_wait_for_next_feed_modern_end_of_data(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._wait_status_mode = "modern"
    mock_adapter.timeout = 0
    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.wait_next_feed.return_value = 1  # end of data

    result = HftBacktestAdapter._wait_for_next_feed(mock_adapter)
    assert result is False


def test_wait_for_next_feed_modern_success(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._wait_status_mode = "modern"
    mock_adapter.timeout = 0
    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.wait_next_feed.return_value = 2  # feed updated

    result = HftBacktestAdapter._wait_for_next_feed(mock_adapter)
    assert result is True


def test_wait_for_next_feed_modern_timeout(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._wait_status_mode = "modern"
    mock_adapter.timeout = 100
    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.wait_next_feed.return_value = 0  # timeout

    with pytest.raises(TimeoutError):
        HftBacktestAdapter._wait_for_next_feed(mock_adapter)


def test_wait_for_next_feed_legacy_success(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._wait_status_mode = "legacy"
    mock_adapter.timeout = 0
    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.wait_next_feed.return_value = 0  # success in legacy

    result = HftBacktestAdapter._wait_for_next_feed(mock_adapter)
    assert result is True


def test_wait_for_next_feed_legacy_unexpected(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    mock_adapter._wait_status_mode = "legacy"
    mock_adapter.timeout = 0
    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.wait_next_feed.return_value = 99  # unexpected

    with pytest.raises(RuntimeError, match="Unexpected legacy"):
        HftBacktestAdapter._wait_for_next_feed(mock_adapter)


# ---------------------------------------------------------------------------
# _make_feature_lookup (line 92)
# ---------------------------------------------------------------------------


def test_make_feature_lookup(mock_adapter):
    from hft_platform.backtest.adapter import HftBacktestAdapter

    timestamps = np.array([100, 200, 300])
    features = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])

    mock_adapter.hbt = MagicMock()
    mock_adapter.hbt.current_timestamp = 250

    lookup = HftBacktestAdapter._make_feature_lookup(mock_adapter, timestamps, features)
    result = lookup("TXFD6")
    assert result == (3.0, 4.0)

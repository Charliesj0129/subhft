"""Unit tests for hft_platform.backtest._elapse_loop.

Tests cover run_elapse() behavior including:
- normal elapse loop iteration
- invalid depth skipping (validate_depth returns False)
- last_trades attachment success and failure paths
- feature event dispatch
- empty loop (hbt.elapse returns non-zero immediately)
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from hft_platform.backtest._elapse_loop import run_elapse
from hft_platform.events import LOBStatsEvent


def _make_lob_event(symbol: str = "TEST", ts: int = 1_000_000) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.1,
        best_bid=100_000,
        best_ask=100_100,
        bid_depth=10,
        ask_depth=8,
    )


def _make_adapter(elapse_results: list[int], *, num_iterations: int | None = None):
    """Build a duck-typed adapter mock.

    elapse_results: return values for sequential calls to hbt.elapse().
    The loop continues while the return value is 0.
    """
    adapter = MagicMock()
    adapter.elapse_ns = 10_000_000  # 10 ms
    adapter.price_scale = 10_000
    adapter.dispatch_feature_events = False

    # hbt mock
    hbt = MagicMock()
    hbt.elapse.side_effect = elapse_results
    hbt.current_timestamp = 1_234_567_890_000_000

    # depth mock (valid depth by default)
    dp = MagicMock()
    dp.best_bid = 1.0  # raw float, will be scaled
    dp.best_ask = 1.01
    hbt.depth.return_value = dp
    hbt.last_trades.return_value = None
    hbt.close.return_value = "closed"

    adapter.hbt = hbt
    return adapter, dp


class TestRunElapseEmptyLoop:
    def test_returns_close_result_when_loop_never_runs(self):
        """If hbt.elapse() returns non-zero immediately, loop body never executes."""
        adapter, _ = _make_adapter(elapse_results=[1])

        result = run_elapse(adapter)

        assert result == "closed"
        adapter._reset_equity_buffers.assert_called_once()
        adapter.hbt.depth.assert_not_called()

    def test_close_called_exactly_once(self):
        adapter, _ = _make_adapter(elapse_results=[1])

        run_elapse(adapter)

        adapter.hbt.close.assert_called_once()


class TestRunElapseInvalidDepth:
    def test_skips_loop_body_when_depth_invalid_nan_bid(self):
        """NaN best_bid causes validate_depth to return False; loop body skips."""
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = float("nan")

        run_elapse(adapter)

        # build_lob_event and dispatch_strategy should NOT be called
        adapter.strategy.handle_event.assert_not_called()

    def test_skips_loop_body_when_best_ask_zero(self):
        """best_ask == 0 makes validate_depth return False."""
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 0.0

        run_elapse(adapter)

        adapter.strategy.handle_event.assert_not_called()

    def test_skips_loop_body_when_bid_greater_than_ask(self):
        """Crossed market causes validate_depth to return False."""
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.02
        dp.best_ask = 1.01

        run_elapse(adapter)

        adapter.strategy.handle_event.assert_not_called()


class TestRunElapseNormalIteration:
    def _setup_adapter_with_event(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        # Use valid depth
        dp.best_bid = 1.0
        dp.best_ask = 1.01
        adapter.hbt.current_timestamp = 5_000_000_000

        event = _make_lob_event()
        feature_event = None

        # Patch helpers so we can control what they return
        adapter.strategy.handle_event.return_value = []
        return adapter, dp, event, feature_event

    def test_process_fills_called_with_correct_args(self):
        adapter, dp, event, feature_event = self._setup_adapter_with_event()
        expected_ts = int(adapter.hbt.current_timestamp)
        expected_bid = int(round(float(dp.best_bid) * adapter.price_scale))
        expected_ask = int(round(float(dp.best_ask) * adapter.price_scale))

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, feature_event)),
            patch("hft_platform.backtest._elapse_loop.process_fills") as mock_fills,
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        mock_fills.assert_called_once_with(adapter, expected_ts, expected_bid, expected_ask)

    def test_dispatch_strategy_called_with_event(self):
        adapter, dp, event, feature_event = self._setup_adapter_with_event()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, feature_event)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy") as mock_dispatch,
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        mock_dispatch.assert_called_once_with(adapter, event, feature_event)

    def test_multiple_iterations_all_processed(self):
        adapter, dp = _make_adapter(elapse_results=[0, 0, 0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01

        event = _make_lob_event()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy") as mock_dispatch,
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        assert mock_dispatch.call_count == 3

    def test_reset_equity_buffers_called_before_loop(self):
        adapter, _ = _make_adapter(elapse_results=[1])

        run_elapse(adapter)

        adapter._reset_equity_buffers.assert_called_once()

    def test_depth_queried_for_asset_0(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01
        event = _make_lob_event()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        adapter.hbt.depth.assert_called_with(0)


class TestRunElapseLastTrades:
    def _make_valid_adapter(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01
        adapter.strategy.handle_event.return_value = []
        return adapter, dp

    def test_last_trades_attached_to_event_when_available(self):
        adapter, dp = self._make_valid_adapter()
        trades = [{"price": 1.0, "qty": 5}]
        adapter.hbt.last_trades.return_value = trades

        # Use a real MagicMock event so setattr is allowed
        event = MagicMock(spec_set=[])  # no spec → allows setattr
        event_obj = MagicMock()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event_obj, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        # clear_last_trades must be called to drain the buffer
        adapter.hbt.clear_last_trades.assert_called_once_with(0)

    def test_last_trades_none_when_hbt_raises_attribute_error(self):
        adapter, dp = self._make_valid_adapter()
        adapter.hbt.last_trades.side_effect = AttributeError("no last_trades")

        event_obj = MagicMock()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event_obj, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            # Must not raise; loop should continue normally
            result = run_elapse(adapter)

        assert result == "closed"

    def test_last_trades_none_when_hbt_raises_type_error(self):
        adapter, dp = self._make_valid_adapter()
        adapter.hbt.last_trades.side_effect = TypeError("wrong type")

        event_obj = MagicMock()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event_obj, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            result = run_elapse(adapter)

        assert result == "closed"

    def test_setattr_failure_on_frozen_event_is_swallowed(self):
        """If the event object rejects setattr (frozen), the loop must continue."""
        adapter, dp = self._make_valid_adapter()
        adapter.hbt.last_trades.return_value = [{"trade": 1}]

        # Simulate a frozen dataclass-like object
        class FrozenEvent:
            def __setattr__(self, name, value):
                raise AttributeError("frozen")

        frozen_event = FrozenEvent()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(frozen_event, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            result = run_elapse(adapter)

        assert result == "closed"


class TestRunElapseFeatureEvents:
    def test_feature_event_passed_to_dispatch_strategy(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01

        event = _make_lob_event()
        feature_event = MagicMock()  # a non-None feature event

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, feature_event)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy") as mock_dispatch,
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        mock_dispatch.assert_called_once_with(adapter, event, feature_event)

    def test_feature_event_none_passed_to_dispatch_strategy(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01
        event = _make_lob_event()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, None)),
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy") as mock_dispatch,
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        mock_dispatch.assert_called_once_with(adapter, event, None)


class TestRunElapseTimestampScaling:
    def test_price_scaled_by_price_scale(self):
        """Prices should be int(round(float(raw) * price_scale))."""
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.23
        dp.best_ask = 1.24
        adapter.price_scale = 10_000
        adapter.hbt.current_timestamp = 9_999_999_000

        event = _make_lob_event()
        expected_bid = int(round(1.23 * 10_000))  # 12300
        expected_ask = int(round(1.24 * 10_000))  # 12400

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, None)) as mock_build,
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        # build_lob_event called with scaled bid/ask
        _args = mock_build.call_args
        assert _args[0][3] == expected_bid
        assert _args[0][4] == expected_ask

    def test_timestamp_is_int_of_current_timestamp(self):
        adapter, dp = _make_adapter(elapse_results=[0, 1])
        dp.best_bid = 1.0
        dp.best_ask = 1.01
        adapter.hbt.current_timestamp = 1_234_567_890_123  # float-like

        event = _make_lob_event()

        with (
            patch("hft_platform.backtest._elapse_loop.build_lob_event", return_value=(event, None)) as mock_build,
            patch("hft_platform.backtest._elapse_loop.process_fills"),
            patch("hft_platform.backtest._elapse_loop.dispatch_strategy"),
            patch("hft_platform.backtest._elapse_loop.validate_depth", return_value=True),
        ):
            run_elapse(adapter)

        _args = mock_build.call_args
        assert _args[0][2] == int(1_234_567_890_123)

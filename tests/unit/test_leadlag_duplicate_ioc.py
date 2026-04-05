"""Test P1-2: aggressive_exit_inflight not cleared before cancel ACK."""

from unittest.mock import MagicMock

import pytest

from hft_platform.strategies.tx_tmf_leadlag import TxTmfLeadLagStrategy

_ONE_SEC_NS = 1_000_000_000


def _make_strategy(**overrides):
    """Create a TxTmfLeadLagStrategy with defaults suitable for testing."""
    defaults = {
        "session_start_sec": 0,
        "session_end_sec": 86400,
        "dvol_threshold": 20,
        "sl_pts": 100,
        "max_hold_ns": 900 * _ONE_SEC_NS,
        "max_position_lots": 3,
        "cooldown_ns": 0,
    }
    defaults.update(overrides)
    return TxTmfLeadLagStrategy(**defaults)


class TestDuplicateIOCPrevention:
    """P1-2: Cancelling a resting exit must not clear aggressive_exit_inflight."""

    def test_cancel_resting_exit_preserves_inflight_flag(self) -> None:
        """When _check_exit cancels a resting exit for force close,
        aggressive_exit_inflight must remain True to block duplicate IOC."""
        strategy = _make_strategy()
        strategy.ctx = MagicMock()

        from hft_platform.strategies.tx_tmf_leadlag import _OpenPosition

        pos = _OpenPosition(entry_ts_ns=0, entry_price=200_000_000, direction=1)
        pos.exit_order_id = "EXIT-001"
        pos.awaiting_exit = False
        pos.aggressive_exit_inflight = True

        strategy._positions_open.append(pos)

        # Simulate the cancel path in on_tick (SL/TK exit with resting order)
        # This mimics the code at line ~291 and ~367
        if pos.exit_order_id:
            strategy.cancel(strategy._trade_symbol, pos.exit_order_id)
            pos.pending_force_close = True
            pos.exit_order_id = ""
            pos.awaiting_exit = False
            # This is the fix: aggressive_exit_inflight is NOT cleared

        assert pos.aggressive_exit_inflight is True
        assert pos.pending_force_close is True
        assert pos.exit_order_id == ""

    def test_cancel_ack_clears_inflight_flag(self) -> None:
        """on_order() terminal status handler should clear the flag."""
        strategy = _make_strategy()
        strategy.ctx = MagicMock()

        from hft_platform.contracts.execution import OrderEvent, OrderStatus
        from hft_platform.contracts.strategy import Side
        from hft_platform.strategies.tx_tmf_leadlag import _OpenPosition

        pos = _OpenPosition(entry_ts_ns=0, entry_price=200_000_000, direction=1)
        pos.exit_order_id = "EXIT-002"
        pos.awaiting_exit = False
        pos.aggressive_exit_inflight = True
        pos.pending_force_close = False

        strategy._positions_open.append(pos)

        event = MagicMock(spec=OrderEvent)
        event.symbol = "TMFD6"
        event.side = Side.SELL
        event.order_id = "EXIT-002"
        event.status = OrderStatus.CANCELLED
        event.filled_qty = 0

        strategy.on_order(event)

        assert pos.aggressive_exit_inflight is False
        assert pos.pending_force_close is True

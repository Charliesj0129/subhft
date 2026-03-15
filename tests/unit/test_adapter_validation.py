"""Tests for adapter crossed-book detection and zero-qty handling.

Bug #4: Crossed book (best_bid >= best_ask) must be skipped.
Bug #6: Zero qty must be preserved, not treated as falsy/missing.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestCrossedBookDetection(unittest.TestCase):
    """Bug #4: adapter.run() must skip ticks where best_bid >= best_ask."""

    def _make_adapter(self):
        """Create an HftBacktestAdapter with mocked hftbacktest imports."""
        mock_hbt_mod = MagicMock()
        mock_order_mod = MagicMock()

        with patch.dict(
            sys.modules,
            {"hftbacktest": mock_hbt_mod, "hftbacktest.order": mock_order_mod},
        ):
            import hft_platform.backtest.adapter as adapter_mod

            importlib.reload(adapter_mod)

            strategy = MagicMock()
            strategy.strategy_id = "test_strat"
            strategy.handle_event = MagicMock(return_value=[])

            adapter = adapter_mod.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="2330",
                data_path="dummy.npz",
            )
            adapter._wait_status_mode = "modern"
            return adapter, strategy

    def test_crossed_book_skipped(self):
        """Crossed book (bid=102, ask=100) must not reach strategy."""
        adapter, strategy = self._make_adapter()

        call_count = 0

        def fake_wait(flag, timeout):
            nonlocal call_count
            call_count += 1
            return 2 if call_count <= 1 else 1

        adapter.hbt.wait_next_feed = fake_wait
        adapter.hbt.current_timestamp = 1_000_000

        depth = MagicMock()
        depth.best_bid = 102
        depth.best_ask = 100  # crossed
        adapter.hbt.depth.return_value = depth

        adapter.run()
        strategy.handle_event.assert_not_called()

    def test_locked_book_skipped(self):
        """Locked book (bid==ask) must not reach strategy."""
        adapter, strategy = self._make_adapter()

        call_count = 0

        def fake_wait(flag, timeout):
            nonlocal call_count
            call_count += 1
            return 2 if call_count <= 1 else 1

        adapter.hbt.wait_next_feed = fake_wait
        adapter.hbt.current_timestamp = 1_000_000

        depth = MagicMock()
        depth.best_bid = 100
        depth.best_ask = 100  # locked
        adapter.hbt.depth.return_value = depth

        adapter.run()
        strategy.handle_event.assert_not_called()

    def test_valid_book_reaches_strategy(self):
        """Normal book (bid < ask) must reach strategy."""
        adapter, strategy = self._make_adapter()

        call_count = 0

        def fake_wait(flag, timeout):
            nonlocal call_count
            call_count += 1
            return 2 if call_count <= 1 else 1

        adapter.hbt.wait_next_feed = fake_wait
        adapter.hbt.current_timestamp = 1_000_000
        adapter.hbt.position.return_value = 0

        depth = MagicMock()
        depth.best_bid = 100
        depth.best_ask = 102  # valid
        adapter.hbt.depth.return_value = depth

        adapter.run()
        strategy.handle_event.assert_called()


class TestZeroQtyHandling(unittest.TestCase):
    """Bug #6: zero qty must be preserved, not treated as missing."""

    def _make_adapter(self):
        mock_hbt_mod = MagicMock()
        mock_order_mod = MagicMock()

        with patch.dict(
            sys.modules,
            {"hftbacktest": mock_hbt_mod, "hftbacktest.order": mock_order_mod},
        ):
            import hft_platform.backtest.adapter as adapter_mod

            importlib.reload(adapter_mod)

            strategy = MagicMock()
            strategy.strategy_id = "test_strat"
            strategy.handle_event = MagicMock(return_value=[])

            adapter = adapter_mod.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="2330",
                data_path="dummy.npz",
            )
            return adapter

    def test_zero_qty_preserved(self):
        """best_bid_qty=0 must produce bid_qty=0, not fall through."""
        adapter = self._make_adapter()

        depth = SimpleNamespace(
            best_bid=100,
            best_ask=102,
            best_bid_qty=0,
            best_ask_qty=5,
        )

        event = adapter._build_l1_bidask_event(depth, ts_ns=1_000_000)
        bid_qty = event.bids[0, 1]
        ask_qty = event.asks[0, 1]
        self.assertEqual(bid_qty, 0, "Zero bid qty must be preserved, not fall through")
        self.assertEqual(ask_qty, 5)

    def test_none_qty_falls_through(self):
        """When best_bid_qty is absent, bid_qty attribute should be used."""
        adapter = self._make_adapter()

        # No best_bid_qty, but has bid_qty
        depth = SimpleNamespace(
            best_bid=100,
            best_ask=102,
            bid_qty=50,
            ask_qty=30,
        )

        event = adapter._build_l1_bidask_event(depth, ts_ns=1_000_000)
        bid_qty = event.bids[0, 1]
        ask_qty = event.asks[0, 1]
        self.assertEqual(bid_qty, 50)
        self.assertEqual(ask_qty, 30)

    def test_all_qty_none_defaults_zero(self):
        """When no qty attributes exist at all, default to 0."""
        adapter = self._make_adapter()

        # Only price attrs, no qty attrs
        depth = SimpleNamespace(
            best_bid=100,
            best_ask=102,
        )

        event = adapter._build_l1_bidask_event(depth, ts_ns=1_000_000)
        bid_qty = event.bids[0, 1]
        ask_qty = event.asks[0, 1]
        self.assertEqual(bid_qty, 0)
        self.assertEqual(ask_qty, 0)

    def test_zero_ask_qty_preserved(self):
        """best_ask_qty=0 must produce ask_qty=0, not fall through."""
        adapter = self._make_adapter()

        depth = SimpleNamespace(
            best_bid=100,
            best_ask=102,
            best_bid_qty=10,
            best_ask_qty=0,
        )

        event = adapter._build_l1_bidask_event(depth, ts_ns=1_000_000)
        bid_qty = event.bids[0, 1]
        ask_qty = event.asks[0, 1]
        self.assertEqual(bid_qty, 10)
        self.assertEqual(ask_qty, 0, "Zero ask qty must be preserved, not fall through")


if __name__ == "__main__":
    unittest.main()

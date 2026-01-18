import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Mock dependencies if missing
try:
    from hft_platform.backtest.adapter import StubContext
    from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side

    NO_DEPS = False
except ImportError:
    NO_DEPS = True


class TestBacktestAdapterDetailed(unittest.TestCase):
    def setUp(self):
        if NO_DEPS:
            self.skipTest("Dependencies missing")

        self.strat = MagicMock()
        self.strat.on_book.return_value = []

    def test_get_mid_price_nan(self):
        """Verify NaN handling when book is empty/max_int."""
        # Clean way: Mock sys.modules logic for hftbacktest
        import sys

        mock_hbt_mod = MagicMock()
        mock_depth_cls = MagicMock()
        mock_hbt_mod.HashMapMarketDepthBacktest = mock_depth_cls

        with patch.dict(sys.modules, {"hftbacktest": mock_hbt_mod, "hftbacktest.order": MagicMock()}):
            # Reload to bind names
            import importlib

            import hft_platform.backtest.adapter

            importlib.reload(hft_platform.backtest.adapter)

            from hft_platform.backtest.adapter import HftBacktestAdapter

            with patch("hft_platform.backtest.adapter.HashMapMarketDepthBacktest") as MockEngine:
                adapter = HftBacktestAdapter(self.strat, "2330", "data.npz")

                # Mock depth
                mock_depth = MagicMock()
                mock_depth.best_bid = 0
                mock_depth.best_ask = 2147483647
                adapter.hbt.depth.return_value = mock_depth

                mid = adapter.get_mid_price()
                self.assertTrue(np.isnan(mid))

                # Valid
                mock_depth.best_bid = 100
                mock_depth.best_ask = 102
                mid = adapter.get_mid_price()
                self.assertEqual(mid, 101.0)

    def test_execute_intent_mapping(self):
        """Verify Intent -> HBT Order mapping."""
        # Clean way: Mock sys.modules logic for hftbacktest
        import sys

        mock_hbt_mod = MagicMock()

        with patch.dict(sys.modules, {"hftbacktest": mock_hbt_mod, "hftbacktest.order": MagicMock()}):
            import importlib

            import hft_platform.backtest.adapter

            importlib.reload(hft_platform.backtest.adapter)
            from hft_platform.backtest.adapter import HftBacktestAdapter

            with patch("hft_platform.backtest.adapter.HashMapMarketDepthBacktest") as MockEngine:
                adapter = HftBacktestAdapter(self.strat, "2330", "data.npz")

                # Buy Limit
                intent = OrderIntent(1, "strat", "2330", IntentType.NEW, Side.BUY, 100.0, 1, TIF.LIMIT, None)

                adapter.execute_intent(intent)

                adapter.hbt.submit_buy_order.assert_called()
                args = adapter.hbt.submit_buy_order.call_args[0]
                # asset=0, id=1, price=100.0, qty=1, tif=ROD(likely), type=Limit
                self.assertEqual(args[1], 1)
                self.assertEqual(args[2], 100.0)

                # Sell IOC
                intent.side = Side.SELL
                intent.tif = TIF.IOC
                adapter.execute_intent(intent)
                adapter.hbt.submit_sell_order.assert_called()

    def test_stub_context(self):
        """Verify StubContext syncs state."""
        hbt = MagicMock()
        hbt.position.return_value = 50

        ctx = StubContext(hbt, "2330")
        ctx.sync_state()

        self.assertEqual(ctx.positions["2330"], 50)

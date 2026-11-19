import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules["hftbacktest"] = MagicMock()

from hft_platform.backtest.adapter import StrategyHbtAdapter
from hft_platform.contracts.strategy import IntentType, OrderIntent, RiskDecision, Side


class TestBacktestSimToReal(unittest.TestCase):
    @patch("hft_platform.backtest.adapter.RiskEngine")
    @patch("hft_platform.backtest.adapter.LOBEngine")
    @patch("hft_platform.backtest.adapter.StrategyRegistry")
    def test_risk_and_lob_integration(self, MockRegistry, MockLOB, MockRisk):
        mock_risk_instance = MockRisk.return_value
        mock_lob_instance = MockLOB.return_value

        mock_strat = MagicMock()
        mock_strat.strategy_id = "test_strat"
        mock_strat.on_book.return_value = [
            OrderIntent(
                strategy_id="test_strat",
                symbol="2330",
                side=Side.BUY,
                price=100.0,
                qty=1,
                intent_type=IntentType.NEW,
                intent_id="1",
            )
        ]

        with patch.object(StrategyHbtAdapter, "_load_strategy", return_value=mock_strat):
            with patch("os.path.exists", return_value=True):
                adapter = StrategyHbtAdapter(
                    data_paths=["/tmp/data.npz"],
                    strategy_module="dummy",
                    strategy_class="dummy",
                    strategy_id="test_strat",
                    symbols=["2330"],
                    risk_config_path="dummy_risk.yaml",
                )

        MockRisk.assert_called()
        MockLOB.assert_called()

        adapter.engine.wait_next_feed.side_effect = [0, -1]
        adapter.engine.depth.return_value.best_bid = 100.0
        adapter.engine.submit_buy_order.return_value = 1
        adapter.engine.submit_sell_order.return_value = 1
        mock_risk_instance.evaluate.return_value = RiskDecision(True, None)

        adapter.run()

        mock_lob_instance.process_event.assert_called()

        mock_risk_instance.evaluate.assert_called()

        adapter.engine.submit_buy_order.assert_called()

        print("Test 1 Passed: LOB Update and Risk Approval")

        adapter.engine.wait_next_feed.side_effect = [0, -1]
        mock_risk_instance.evaluate.return_value = RiskDecision(False, None, "REJECTED")
        adapter.engine.submit_buy_order.reset_mock()

        adapter.run()

        adapter.engine.submit_buy_order.assert_not_called()
        print("Test 2 Passed: Risk Rejection blocks order")


if __name__ == "__main__":
    unittest.main()

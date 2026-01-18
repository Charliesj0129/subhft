import asyncio
import time
import unittest
from unittest.mock import MagicMock

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter


class TestOrderLifecycle(unittest.TestCase):
    @unittest.mock.patch("hft_platform.order.adapter.OrderAdapter.load_config")
    def test_lifecycle(self, mock_load):
        # Setup
        queue = asyncio.Queue()
        client = MagicMock()
        client.get_exchange.return_value = "TSE"
        client.place_order.return_value = {"seq_no": "123"}

        adapter = OrderAdapter("config/dummy.yaml", queue, client)

        # 1. Create Order
        intent = OrderIntent(
            intent_id=1,
            strategy_id="strat1",
            symbol="2330",
            side=Side.BUY,
            price=100,
            qty=1,
            intent_type=IntentType.NEW,
            tif=TIF.LIMIT,
        )
        cmd = OrderCommand(1, intent, time.time_ns() + 1e9, 0)

        # Execute
        asyncio.run(adapter.execute(cmd))

        # Verify added to live_orders
        key = "strat1:1"
        self.assertIn(key, adapter.live_orders)
        self.assertEqual(adapter.order_id_map["123"], key)
        print("Test Step 1: Order added to live_orders and map populated.")

        # 2. Simulate Terminal State (Filled)
        # Main would call on_terminal_state
        adapter.on_terminal_state("strat1", "1")

        # Verify removed
        self.assertNotIn(key, adapter.live_orders)
        print("Test Step 2: Order removed from live_orders on terminal state.")

    @unittest.mock.patch("hft_platform.order.adapter.OrderAdapter.load_config")
    def test_cancel_flow(self, mock_load):
        queue = asyncio.Queue()
        client = MagicMock()
        client.get_exchange.return_value = "TSE"
        mock_trade = {"seq_no": "456"}
        client.place_order.return_value = mock_trade

        adapter = OrderAdapter("config/dummy.yaml", queue, client)

        # Place Parent
        intent = OrderIntent(
            intent_id=2,
            strategy_id="strat1",
            symbol="2330",
            side=Side.BUY,
            price=100,
            qty=1,
            intent_type=IntentType.NEW,
            tif=TIF.LIMIT,
        )
        cmd = OrderCommand(2, intent, time.time_ns() + 1e9, 0)
        asyncio.run(adapter.execute(cmd))

        # Cancel
        cancel_intent = OrderIntent(
            intent_id=3,
            strategy_id="strat1",
            symbol="2330",
            side=Side.BUY,
            price=0,
            qty=0,
            intent_type=IntentType.CANCEL,
            target_order_id=2,
        )
        cancel_cmd = OrderCommand(3, cancel_intent, time.time_ns() + 1e9, 0)
        asyncio.run(adapter.execute(cancel_cmd))

        client.cancel_order.assert_called_with(mock_trade)
        print("Test Step 3: Cancel calls client correctly using live_orders look up.")


if __name__ == "__main__":
    unittest.main()

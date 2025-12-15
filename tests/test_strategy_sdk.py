
import unittest
from unittest.mock import MagicMock
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.contracts.strategy import Side, IntentType
from hft_platform.strategies.simple_strategy import SimpleStrategy

class TestStrategySDK(unittest.TestCase):
    def test_simple_strategy_logic(self):
        # 1. Setup
        strat = SimpleStrategy("test-strat")
        
        # Mock Context
        mock_ctx = MagicMock(spec=StrategyContext)
        mock_ctx.positions = {"2330": 0}
        mock_ctx.get_features.return_value = {"mid_price": 100.0, "spread": 2.0}
        
        # Mock place_order factory behavior
        def mock_place(**kwargs):
            # Return dict or mock object acting as Intent
            return kwargs
        mock_ctx.place_order.side_effect = mock_place
        
        # 2. Simulate Event (Book Update)
        # LOBEngine likely emits dicts like {"symbol": "2330", "mid_price": 100.0, ...}
        event = {
            "symbol": "2330", 
            "mid_price": 100.0, 
            "spread": 2.0,
            "type": "snapshot"
        }
        
        # 3. Execution
        generated_intents = strat.on_book(mock_ctx, event)
        
        # 4. Verification
        self.assertEqual(len(generated_intents), 1)
        intent = generated_intents[0]
        
        # Check intent details
        self.assertEqual(intent['symbol'], "2330")
        self.assertEqual(intent['side'], Side.BUY)
        self.assertEqual(intent['price'], 99.0) # mid - 1
        print("SDK Test Passed: Generated correct intent from high-level API.")

if __name__ == "__main__":
    unittest.main()

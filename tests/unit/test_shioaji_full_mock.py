import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

# IMPORTANT: mocking BEFORE import is hard if module imports at top level.
# shioaji_client tries to import shioaji inside try-except.
# We will mock the module `shioaji` in `sys.modules` or rely on `sj` being None check if we can't install it.
# However, we want to test the logic when `sj` IS present.
# So we must patch `hft_platform.feed_adapter.shioaji_client.sj`
from hft_platform.feed_adapter.shioaji_client import ShioajiClient


class TestShioajiClientFull(unittest.TestCase):
    def setUp(self):
        # Create temp config
        self.tmp_config = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(
            {"symbols": [{"code": "2330", "exchange": "TSE"}, {"code": "TXFA", "exchange": "FUT"}]}, self.tmp_config
        )
        self.tmp_config.close()

        # Patch 'sj' in the module
        self.patcher = patch("hft_platform.feed_adapter.shioaji_client.sj")
        self.mock_sj_mod = self.patcher.start()

        # Configure Mock Shioaji class
        self.mock_api_instance = MagicMock()
        self.mock_sj_mod.Shioaji.return_value = self.mock_api_instance

        # Constants
        self.mock_sj_mod.constant.QuoteType.Tick = "tick"
        self.mock_sj_mod.constant.QuoteType.BidAsk = "bidask"
        self.mock_sj_mod.constant.Action.Buy = "Buy"
        self.mock_sj_mod.constant.Action.Sell = "Sell"
        self.mock_sj_mod.constant.StockPriceType.LMT = "LMT"
        self.mock_sj_mod.constant.OrderType.ROD = "ROD"
        self.mock_sj_mod.constant.OrderType.IOC = "IOC"
        self.mock_sj_mod.constant.OrderType.FOK = "FOK"

        self.client = ShioajiClient(config_path=self.tmp_config.name)
        # Client init tries to create Shioaji() if sj is present
        # We need to ensure self.client.api is our mock
        self.client.api = self.mock_api_instance

        # Mock Contracts lookup structure
        # Contracts.Stocks.TSE["2330"] etc
        self.mock_contract_2330 = MagicMock()
        self.mock_contract_2330.code = "2330"
        self.mock_contract_txfa = MagicMock()

        self.mock_api_instance.Contracts.Stocks.TSE = {"2330": self.mock_contract_2330}
        self.mock_api_instance.Contracts.Futures = {"TXFA": self.mock_contract_txfa}

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.tmp_config.name)

    def test_login_flow(self):
        # Test Env var login
        with patch.dict(os.environ, {"SHIOAJI_PERSON_ID": "TESTID", "SHIOAJI_PASSWORD": "TESTPW"}):
            self.client.login()
            self.mock_api_instance.login.assert_called_once()
            _, kwargs = self.mock_api_instance.login.call_args
            self.assertEqual(kwargs["person_id"], "TESTID")
            self.assertEqual(kwargs["passwd"], "TESTPW")
            self.assertIsNone(kwargs.get("contracts_cb"))
            self.assertTrue(self.client.logged_in)

    def test_subscribe_basket(self):
        self.client.logged_in = True
        cb = MagicMock()
        self.client.subscribe_basket(cb)

        # Should lookup 2330 and TXFA
        # 2330 in config is TSE
        # TXFA in config is FUT

        # Verify subscriptions
        # 2 symbols * 2 quote types = 4 subscribes
        self.assertEqual(self.mock_api_instance.quote.subscribe.call_count, 4)
        self.mock_api_instance.quote.set_on_tick_stk_v1_callback.assert_called_with(cb)

    def test_place_order(self):
        self.client.place_order("2330", "TSE", "Buy", 100.0, 1, "ROD", "Regular")

        # Verify sj.Order constructor called correctly
        self.mock_sj_mod.Order.assert_called()
        call_args = self.mock_sj_mod.Order.call_args
        kwargs = call_args[1]
        self.assertEqual(kwargs["price"], 100.0)
        self.assertEqual(kwargs["quantity"], 1)
        self.assertEqual(kwargs["action"], "Buy")
        self.assertEqual(kwargs["price_type"], "LMT")
        self.assertEqual(kwargs["order_type"], "ROD")

        # Verify api.place_order called with result of sj.Order
        self.mock_api_instance.place_order.assert_called()

    def test_set_execution_callbacks(self):
        on_order = MagicMock()
        on_deal = MagicMock()
        self.client.set_execution_callbacks(on_order, on_deal)

        self.mock_api_instance.set_order_callback.assert_called_once()
        callback = self.mock_api_instance.set_order_callback.call_args[0][0]
        self.assertTrue(callable(callback))
        self.mock_api_instance.set_deal_callback.assert_not_called()

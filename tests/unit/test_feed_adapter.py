from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji_client import ShioajiClient


@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")
    c = ShioajiClient(str(cfg))
    # inject mock api
    c.api = MagicMock()
    return c


def test_config_loading(client):
    assert len(client.symbols) == 1


def test_login_args(client):
    client.login(api_key="K", secret_key="S")
    client.api.login.assert_called()


def test_subscribe_basket(client):
    client.logged_in = True

    # Mock return for Contracts lookup
    # Shioaji contracts are accessed via __getitem__ usually
    # api.Contracts.Stocks.TSE['2330']

    # We mock the Contract object itself
    mock_contract = MagicMock()
    # If using real Shioaji Types, Contract is a pydantic model.
    # But for subscription, api just needs an object to pass to subscribe.

    client.api.Contracts.Stocks.TSE.__getitem__.return_value = mock_contract

    cb = MagicMock()
    client.subscribe_basket(cb)

    client.api.quote.subscribe.assert_called()


def test_place_order_wrapper(client):
    client.logged_in = True

    # Shioaji uses Pydantic models for Order. Construction validation is strict.
    # We mock 'shioaji.Order' class so we don't need real pydantic validation logic,
    # or we construct a mock that mimics the behavior.

    # We also need to patch the enums so client.place_order can access them.
    # The client imports 'shioaji' as 'sj'.

    # The clean way: Patch 'hft_platform.feed_adapter.shioaji_client.sj' inside the module

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        # 1. Setup Mock Enums
        mock_sj.constant.Action.Buy = "Buy"
        mock_sj.constant.StockPriceType.LMT = "LMT"
        mock_sj.constant.OrderType.ROD = "ROD"

        # 2. Setup Mock Order Constructor
        # It should return a mock order object
        mock_order_instance = MagicMock()
        mock_sj.Order.return_value = mock_order_instance

        # 3. Setup Contract Lookup
        # Client calls self._get_contract(code, exchange)
        with patch.object(client, "_get_contract") as mock_get_contract:
            mock_contract = MagicMock()
            mock_get_contract.return_value = mock_contract

            # 4. Invoke
            client.place_order("2330", "TSE", "Buy", 100.0, 1, "LMT", "ROD")

            # 5. Verify
            # Verify Order was constructed
            mock_sj.Order.assert_called()

            # Verify API call
            client.api.place_order.assert_called_with(mock_contract, mock_order_instance)

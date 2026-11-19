from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from tests.unit.shioaji_contracts_helper import mark_contracts_loaded


@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")
    c = ShioajiClient(str(cfg))
    c.api = MagicMock()
    return c


def test_config_loading(client):
    assert len(client.symbols) == 1


def test_hft_symbols_filters_loaded_symbols(tmp_path, monkeypatch):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text(
        "symbols:\n"
        "  - code: '2330'\n"
        "    exchange: 'TSE'\n"
        "  - code: 'TMFR1'\n"
        "    exchange: 'TAIFEX'\n"
        "  - code: 'TXO35500Q6'\n"
        "    exchange: 'OPT'\n"
    )
    monkeypatch.setenv("HFT_SYMBOLS", "TMFR1")

    c = ShioajiClient(str(cfg))

    try:
        assert [s["code"] for s in c.symbols] == ["TMFR1"]
        assert c.code_exchange_map == {"TMFR1": "TAIFEX"}
    finally:
        c.close()


def test_hft_symbols_expands_front_month_alias(tmp_path, monkeypatch):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text(
        "symbols:\n"
        "  - code: '2330'\n"
        "    exchange: 'TSE'\n"
        "    type: stock\n"
        "  - code: 'TMFE6'\n"
        "    exchange: 'FUT'\n"
        "    type: future\n"
        "    tags: [futures, front_month, tmf]\n"
        "  - code: 'TMFG6'\n"
        "    exchange: 'FUT'\n"
        "    type: future\n"
        "    tags: [futures, far_month, tmf]\n"
    )
    monkeypatch.setenv("HFT_SYMBOLS", "TMFR1")

    c = ShioajiClient(str(cfg))

    try:
        assert [s["code"] for s in c.symbols] == ["TMFE6"]
        assert c.code_exchange_map == {"TMFE6": "FUT"}
    finally:
        c.close()


def test_login_args(client):
    mark_contracts_loaded(client.api)
    client.login(api_key="K", secret_key="S")
    client.api.login.assert_called()


def test_subscribe_basket(client):
    client.logged_in = True

    mock_contract = MagicMock()

    client.api.Contracts.Stocks.TSE.__getitem__.return_value = mock_contract

    cb = MagicMock()
    client.subscribe_basket(cb)

    client.api.subscribe.assert_called()


def test_place_order_wrapper(client):
    client.logged_in = True

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_sj.constant.Action.Buy = "Buy"
        mock_sj.constant.StockPriceType.LMT = "LMT"
        mock_sj.constant.OrderType.ROD = "ROD"

        mock_order_instance = MagicMock()
        mock_sj.Order.return_value = mock_order_instance

        with patch.object(client, "_get_contract") as mock_get_contract:
            mock_contract = MagicMock()
            mock_get_contract.return_value = mock_contract

            client.place_order("2330", "TSE", "Buy", 100.0, 1, "LMT", "ROD")

            mock_sj.Order.assert_called()

            client.api.place_order.assert_called_with(mock_contract, mock_order_instance)

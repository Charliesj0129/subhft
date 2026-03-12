"""Tests for SubscriptionManager (Phase-6 decoupling)."""

from unittest.mock import MagicMock, patch

import yaml

from hft_platform.feed_adapter.shioaji_client import ShioajiClient


def _make_client(tmp_path):
    """Create a ShioajiClient with mocked Shioaji API."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(yaml.dump({"symbols": [{"code": "2330", "exchange": "TSE"}, {"code": "TXFA", "exchange": "FUT"}]}))

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_api = MagicMock()
        mock_sj.Shioaji.return_value = mock_api
        mock_sj.constant.QuoteType.Tick = "tick"
        mock_sj.constant.QuoteType.BidAsk = "bidask"
        mock_sj.constant.QuoteVersion.v0 = "v0"
        mock_sj.constant.QuoteVersion.v1 = "v1"
        mock_sj.constant.OrderState = MagicMock()

        client = ShioajiClient(config_path=str(cfg))
        client.api = mock_api
        client.metrics = MagicMock()

        mock_contract_2330 = MagicMock()
        mock_contract_2330.code = "2330"
        mock_contract_txfa = MagicMock()
        mock_api.Contracts.Stocks.TSE = {"2330": mock_contract_2330}
        mock_api.Contracts.Futures = {"TXFA": mock_contract_txfa}

    return client, mock_api, mock_sj


class TestSubscribeBasket:
    def test_subscribe_basket_subscribes_all_symbols(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = True
        cb = MagicMock()

        client.subscribe_basket(cb)

        # 2 symbols x 2 quote types = 4 subscribe calls
        assert mock_api.quote.subscribe.call_count == 4
        assert client.tick_callback is cb

    def test_subscribe_basket_skips_when_not_logged_in(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = False
        cb = MagicMock()

        client.subscribe_basket(cb)

        mock_api.quote.subscribe.assert_not_called()

    def test_subscribe_basket_skips_when_no_api(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.api = None
        cb = MagicMock()

        client.subscribe_basket(cb)

        mock_api.quote.subscribe.assert_not_called()

    def test_subscribe_basket_delegates_to_subscription_manager(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = True
        cb = MagicMock()

        with patch.object(SubscriptionManager, "subscribe_basket") as mock_sub:
            client.subscribe_basket(cb)

        mock_sub.assert_called_once_with(cb)


class TestResubscribe:
    def test_resubscribe_delegates_to_subscription_manager(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = True
        client.tick_callback = MagicMock()
        client._last_resubscribe_ts = 0.0

        ok = client.resubscribe()

        assert ok is True

    def test_resubscribe_returns_false_when_not_logged_in(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = False
        client.tick_callback = MagicMock()

        ok = client.resubscribe()

        assert ok is False

    def test_resubscribe_returns_false_when_no_callback(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.logged_in = True
        client.tick_callback = None

        ok = client.resubscribe()

        assert ok is False


class TestSetExecutionCallbacks:
    def test_set_execution_callbacks_registers_on_api(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        on_order = MagicMock()
        on_deal = MagicMock()

        client.set_execution_callbacks(on_order, on_deal)

        mock_api.set_order_callback.assert_called_once()
        callback = mock_api.set_order_callback.call_args[0][0]
        assert callable(callback)

    def test_set_execution_callbacks_skips_when_no_api(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        client.api = None

        # Should not raise
        client.set_execution_callbacks(MagicMock(), MagicMock())

        mock_api.set_order_callback.assert_not_called()

    def test_set_execution_callbacks_delegates_to_manager(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client, mock_api, _ = _make_client(tmp_path)
        on_order = MagicMock()
        on_deal = MagicMock()

        with patch.object(SubscriptionManager, "set_execution_callbacks") as mock_set:
            client.set_execution_callbacks(on_order, on_deal)

        mock_set.assert_called_once_with(on_order=on_order, on_deal=on_deal)


class TestSubscribeSymbol:
    def test_subscribe_symbol_success(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        sym = {"code": "2330", "exchange": "TSE"}

        ok = client._subscribe_symbol(sym, MagicMock())

        assert ok is True
        assert mock_api.quote.subscribe.call_count == 2

    def test_subscribe_symbol_invalid_entry(self, tmp_path):
        client, mock_api, _ = _make_client(tmp_path)
        sym = {"code": "", "exchange": ""}

        ok = client._subscribe_symbol(sym, MagicMock())

        assert ok is False

    def test_unsubscribe_symbol_delegates(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        client, mock_api, _ = _make_client(tmp_path)
        sym = {"code": "2330", "exchange": "TSE"}

        with patch.object(SubscriptionManager, "_unsubscribe_symbol") as mock_unsub:
            client._unsubscribe_symbol(sym)

        mock_unsub.assert_called_once_with(sym)


class TestFacadeWiresSubscriptionManager:
    def test_facade_exposes_subscription_manager(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            facade = ShioajiClientFacade(str(cfg), {})

            assert facade.subscription_manager is not None
            assert facade.subscription_manager is facade._client._subscription_manager

    def test_facade_subscribe_basket_routes_to_manager(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            facade = ShioajiClientFacade(str(cfg), {})

            with patch.object(SubscriptionManager, "subscribe_basket") as mock_sub:
                facade.subscribe_basket(MagicMock())

            mock_sub.assert_called_once()

    def test_facade_set_execution_callbacks_routes_to_manager(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            facade = ShioajiClientFacade(str(cfg), {})
            on_order = MagicMock()
            on_deal = MagicMock()

            with patch.object(SubscriptionManager, "set_execution_callbacks") as mock_set:
                facade.set_execution_callbacks(on_order, on_deal)

            mock_set.assert_called_once_with(on_order=on_order, on_deal=on_deal)

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade


def test_facade_exposes_runtimes_and_gateways(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_api = MagicMock()
        mock_sj.Shioaji.return_value = mock_api

        facade = ShioajiClientFacade(str(cfg), {})

        assert facade.session_runtime is not None
        assert facade.quote_runtime is not None
        assert facade.contracts_runtime is not None
        assert facade.order_gateway is not None
        assert facade.account_gateway is not None
        # Facade reuses the runtime instances created by ShioajiClient.__init__.
        assert facade.session_runtime is facade._client._session_runtime
        assert facade.quote_runtime is facade._client._quote_runtime
        assert facade._client._session_policy is facade.session_runtime
        assert facade._client._quote_event_handler is facade.quote_runtime._event_handler


def test_session_runtime_login_calls_login_with_retry(tmp_path):
    """session_runtime.login() now calls login_with_retry() directly (Phase-2)."""
    from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime

    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_api = MagicMock()
        mock_sj.Shioaji.return_value = mock_api

        facade = ShioajiClientFacade(str(cfg), {})

        with patch.object(SessionRuntime, "login_with_retry", return_value=True) as mock_lwr:
            ok = facade.session_runtime.login()

        assert ok is True
        mock_lwr.assert_called_once()


def test_facade_explicit_delegation_without_getattr(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

    with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
        mock_api = MagicMock()
        mock_sj.Shioaji.return_value = mock_api

        facade = ShioajiClientFacade(str(cfg), {})
        with (
            patch.object(type(facade.session_runtime), "request_reconnect", return_value=True) as mock_reconnect,
            patch.object(type(facade.order_gateway), "place_order", return_value={"seq_no": "S1"}) as mock_place,
        ):
            assert "__getattr__" not in ShioajiClientFacade.__dict__
            assert facade.reconnect(reason="unit", force=True) is True
            assert facade.place_order("2330", "TSE", "Buy", 100.0, 1, "ROD", "Regular") == {"seq_no": "S1"}
            mock_reconnect.assert_called_once_with(reason="unit", force=True)
            mock_place.assert_called_once()

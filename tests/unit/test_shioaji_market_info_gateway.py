from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.market_info_gateway import MarketInfoGateway


def _make_client(*, mode: str = "live", logged_in: bool = True) -> MagicMock:
    """Build a minimal ShioajiClient mock with cache/rate-limit/latency helpers."""
    client = MagicMock()
    client.mode = mode
    client.logged_in = logged_in
    client.api = MagicMock() if logged_in else None
    client._cache_get = MagicMock(return_value=None)
    client._cache_set = MagicMock()
    client._rate_limit_api = MagicMock(return_value=True)
    client._record_api_latency = MagicMock()
    client._get_contract = MagicMock(side_effect=lambda ex, code, **kw: MagicMock(code=code))
    return client


# ---------------------------------------------------------------------------
# get_credit_enquires
# ---------------------------------------------------------------------------


class TestGetCreditEnquires:
    def test_returns_empty_in_simulation(self):
        client = _make_client(mode="simulation")
        gw = MarketInfoGateway(client)
        assert gw.get_credit_enquires(["2330"], "TSE") == []
        client.api.credit_enquires.assert_not_called()

    def test_returns_cached_value(self):
        client = _make_client()
        client._cache_get.return_value = [{"code": "2330", "credit": True}]
        gw = MarketInfoGateway(client)
        result = gw.get_credit_enquires(["2330"], "TSE")
        assert result == [{"code": "2330", "credit": True}]
        client.api.credit_enquires.assert_not_called()

    def test_calls_api_and_caches(self):
        client = _make_client()
        expected = [MagicMock(code="2330")]
        client.api.credit_enquires.return_value = expected
        gw = MarketInfoGateway(client)
        result = gw.get_credit_enquires(["2330"], "TSE", timeout=10000)
        assert len(result) == 1
        client.api.credit_enquires.assert_called_once()
        client._cache_set.assert_called_once()
        client._record_api_latency.assert_called_once()
        # Verify latency recorded as ok
        call_args, call_kwargs = client._record_api_latency.call_args
        assert call_args[0] == "credit_enquires"
        assert call_kwargs.get("ok", True) is True

    def test_returns_empty_when_no_contracts_resolved(self):
        client = _make_client()
        client._get_contract.return_value = None
        gw = MarketInfoGateway(client)
        assert gw.get_credit_enquires(["INVALID"], "TSE") == []

    def test_returns_empty_on_rate_limit(self):
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = MarketInfoGateway(client)
        assert gw.get_credit_enquires(["2330"], "TSE") == []

    def test_returns_empty_on_api_error(self):
        client = _make_client()
        client.api.credit_enquires.side_effect = RuntimeError("network")
        gw = MarketInfoGateway(client)
        result = gw.get_credit_enquires(["2330"], "TSE")
        assert result == []
        # Latency recorded as failed
        call_args, call_kwargs = client._record_api_latency.call_args
        assert call_args[0] == "credit_enquires"
        assert call_kwargs.get("ok") is False

    def test_returns_empty_when_not_logged_in(self):
        client = _make_client(logged_in=False)
        gw = MarketInfoGateway(client)
        assert gw.get_credit_enquires(["2330"], "TSE") == []

    def test_passes_product_type_to_contract_resolution(self):
        client = _make_client()
        client.api.credit_enquires.return_value = []
        gw = MarketInfoGateway(client)
        gw.get_credit_enquires(["2330"], "TSE", product_type="stock")
        client._get_contract.assert_called_once_with(
            "TSE", "2330", product_type="stock", allow_synthetic=False
        )


# ---------------------------------------------------------------------------
# get_short_stock_sources
# ---------------------------------------------------------------------------


class TestGetShortStockSources:
    def test_returns_empty_in_simulation(self):
        client = _make_client(mode="simulation")
        gw = MarketInfoGateway(client)
        assert gw.get_short_stock_sources(["2330"], "TSE") == []

    def test_returns_cached_value(self):
        client = _make_client()
        client._cache_get.return_value = [{"code": "2330", "qty": 100}]
        gw = MarketInfoGateway(client)
        result = gw.get_short_stock_sources(["2330"], "TSE")
        assert result == [{"code": "2330", "qty": 100}]

    def test_calls_api_and_caches(self):
        client = _make_client()
        expected = [MagicMock(code="2330")]
        client.api.short_stock_sources.return_value = expected
        gw = MarketInfoGateway(client)
        result = gw.get_short_stock_sources(["2330"], "TSE", timeout=3000)
        assert len(result) == 1
        client.api.short_stock_sources.assert_called_once()
        client._cache_set.assert_called_once()

    def test_returns_empty_when_no_contracts_resolved(self):
        client = _make_client()
        client._get_contract.return_value = None
        gw = MarketInfoGateway(client)
        assert gw.get_short_stock_sources(["INVALID"], "TSE") == []

    def test_returns_empty_on_rate_limit(self):
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = MarketInfoGateway(client)
        assert gw.get_short_stock_sources(["2330"], "TSE") == []

    def test_returns_empty_on_api_error(self):
        client = _make_client()
        client.api.short_stock_sources.side_effect = RuntimeError("timeout")
        gw = MarketInfoGateway(client)
        assert gw.get_short_stock_sources(["2330"], "TSE") == []

    def test_returns_empty_when_not_logged_in(self):
        client = _make_client(logged_in=False)
        gw = MarketInfoGateway(client)
        assert gw.get_short_stock_sources(["2330"], "TSE") == []


# ---------------------------------------------------------------------------
# get_punish_stocks
# ---------------------------------------------------------------------------


class TestGetPunishStocks:
    def test_returns_empty_in_simulation(self):
        client = _make_client(mode="simulation")
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == []

    def test_returns_cached_value(self):
        client = _make_client()
        client._cache_get.return_value = [{"code": "1234"}]
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == [{"code": "1234"}]

    def test_calls_api_and_caches(self):
        client = _make_client()
        expected = [MagicMock(code="1234")]
        client.api.punish.return_value = expected
        gw = MarketInfoGateway(client)
        result = gw.get_punish_stocks(timeout=2000)
        assert result == expected
        client.api.punish.assert_called_once_with(timeout=2000)
        client._cache_set.assert_called_once()

    def test_returns_empty_on_rate_limit(self):
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == []

    def test_returns_empty_on_api_error(self):
        client = _make_client()
        client.api.punish.side_effect = RuntimeError("fail")
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == []

    def test_returns_empty_when_not_logged_in(self):
        client = _make_client(logged_in=False)
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == []

    def test_handles_none_api_result(self):
        client = _make_client()
        client.api.punish.return_value = None
        gw = MarketInfoGateway(client)
        assert gw.get_punish_stocks() == []


# ---------------------------------------------------------------------------
# get_notice_stocks
# ---------------------------------------------------------------------------


class TestGetNoticeStocks:
    def test_returns_empty_in_simulation(self):
        client = _make_client(mode="simulation")
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == []

    def test_returns_cached_value(self):
        client = _make_client()
        client._cache_get.return_value = [{"code": "5678"}]
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == [{"code": "5678"}]

    def test_calls_api_and_caches(self):
        client = _make_client()
        expected = [MagicMock(code="5678")]
        client.api.notice.return_value = expected
        gw = MarketInfoGateway(client)
        result = gw.get_notice_stocks(timeout=3000)
        assert result == expected
        client.api.notice.assert_called_once_with(timeout=3000)
        client._cache_set.assert_called_once()

    def test_returns_empty_on_rate_limit(self):
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == []

    def test_returns_empty_on_api_error(self):
        client = _make_client()
        client.api.notice.side_effect = RuntimeError("fail")
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == []

    def test_returns_empty_when_not_logged_in(self):
        client = _make_client(logged_in=False)
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == []

    def test_handles_none_api_result(self):
        client = _make_client()
        client.api.notice.return_value = None
        gw = MarketInfoGateway(client)
        assert gw.get_notice_stocks() == []


# ---------------------------------------------------------------------------
# Facade integration
# ---------------------------------------------------------------------------


class TestFacadeWiring:
    def test_facade_exposes_market_info_gateway(self, tmp_path):
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})
            assert facade.market_info_gateway is not None
            assert isinstance(facade.market_info_gateway, MarketInfoGateway)

    def test_facade_delegates_get_credit_enquires(self, tmp_path):
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})
            with patch.object(
                MarketInfoGateway, "get_credit_enquires", return_value=[{"x": 1}]
            ) as m:
                result = facade.get_credit_enquires(["2330"], "TSE", timeout=1000)
                assert result == [{"x": 1}]
                m.assert_called_once_with(["2330"], "TSE", timeout=1000, product_type=None)

    def test_facade_delegates_get_punish_stocks(self, tmp_path):
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})
            with patch.object(
                MarketInfoGateway, "get_punish_stocks", return_value=[]
            ) as m:
                result = facade.get_punish_stocks(timeout=2000)
                assert result == []
                m.assert_called_once_with(timeout=2000)

    def test_facade_delegates_get_notice_stocks(self, tmp_path):
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})
            with patch.object(
                MarketInfoGateway, "get_notice_stocks", return_value=[]
            ) as m:
                result = facade.get_notice_stocks(timeout=3000)
                assert result == []
                m.assert_called_once_with(timeout=3000)

    def test_facade_delegates_get_short_stock_sources(self, tmp_path):
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})
            with patch.object(
                MarketInfoGateway, "get_short_stock_sources", return_value=[]
            ) as m:
                result = facade.get_short_stock_sources(["2330"], "TSE")
                assert result == []
                m.assert_called_once_with(["2330"], "TSE", timeout=5000, product_type=None)


# ---------------------------------------------------------------------------
# _resolve_contracts
# ---------------------------------------------------------------------------


class TestResolveContracts:
    def test_resolves_multiple_codes(self):
        client = _make_client()
        gw = MarketInfoGateway(client)
        contracts = gw._resolve_contracts(["2330", "2317"], "TSE")
        assert len(contracts) == 2
        assert client._get_contract.call_count == 2

    def test_skips_unresolvable_codes(self):
        client = _make_client()
        client._get_contract.side_effect = lambda ex, code, **kw: (
            MagicMock(code=code) if code == "2330" else None
        )
        gw = MarketInfoGateway(client)
        contracts = gw._resolve_contracts(["2330", "INVALID"], "TSE")
        assert len(contracts) == 1

    def test_passes_product_type(self):
        client = _make_client()
        gw = MarketInfoGateway(client)
        gw._resolve_contracts(["2330"], "TSE", product_type="stock")
        client._get_contract.assert_called_once_with(
            "TSE", "2330", product_type="stock", allow_synthetic=False
        )

"""Tests for RiskScreeningGateway (punish, notice, credit, short stock sources)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.risk_screening import (
    RiskScreeningGateway,
    _CREDIT_TTL_S,
    _NOTICE_TTL_S,
    _PUNISH_TTL_S,
    _SHORT_STOCK_TTL_S,
)


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.mode = "live"
    client.logged_in = True
    client._cache_get.return_value = None
    client._rate_limit_api.return_value = True
    return client


@pytest.fixture()
def gateway(mock_client: MagicMock) -> RiskScreeningGateway:
    return RiskScreeningGateway(mock_client)


# ── Simulation mode returns defaults ──────────────────────────────

class TestSimulationMode:
    def test_punish_sim_returns_none(self, mock_client: MagicMock) -> None:
        mock_client.mode = "simulation"
        gw = RiskScreeningGateway(mock_client)
        assert gw.get_punish_stocks() is None

    def test_notice_sim_returns_none(self, mock_client: MagicMock) -> None:
        mock_client.mode = "simulation"
        gw = RiskScreeningGateway(mock_client)
        assert gw.get_notice_stocks() is None

    def test_credit_sim_returns_empty(self, mock_client: MagicMock) -> None:
        mock_client.mode = "simulation"
        gw = RiskScreeningGateway(mock_client)
        assert gw.get_credit_enquiries([MagicMock()]) == []

    def test_short_stock_sim_returns_empty(self, mock_client: MagicMock) -> None:
        mock_client.mode = "simulation"
        gw = RiskScreeningGateway(mock_client)
        assert gw.get_short_stock_sources([MagicMock()]) == []


# ── Cache hit returns cached value ────────────────────────────────

class TestCacheHit:
    def test_punish_cache_hit(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._cache_get.return_value = ["STOCK_A"]
        result = gateway.get_punish_stocks()
        assert result == ["STOCK_A"]
        mock_client.api.punish.assert_not_called()

    def test_notice_cache_hit(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._cache_get.return_value = ["STOCK_B"]
        result = gateway.get_notice_stocks()
        assert result == ["STOCK_B"]
        mock_client.api.notice.assert_not_called()

    def test_credit_cache_hit(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._cache_get.return_value = [{"credit": 100}]
        result = gateway.get_credit_enquiries([MagicMock()])
        assert result == [{"credit": 100}]
        mock_client.api.credit_enquires.assert_not_called()

    def test_short_stock_cache_hit(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._cache_get.return_value = [{"source": "X"}]
        result = gateway.get_short_stock_sources([MagicMock()])
        assert result == [{"source": "X"}]
        mock_client.api.short_stock_sources.assert_not_called()


# ── Cache miss calls API and caches result ────────────────────────

class TestCacheMiss:
    def test_punish_calls_api(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.punish.return_value = ["P1", "P2"]
        result = gateway.get_punish_stocks()
        assert result == ["P1", "P2"]
        mock_client._cache_set.assert_called_once_with("punish", _PUNISH_TTL_S, ["P1", "P2"])
        mock_client._record_api_latency.assert_called_once()
        args = mock_client._record_api_latency.call_args
        assert args[0][0] == "punish"
        assert args[1]["ok"] is True

    def test_notice_calls_api(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.notice.return_value = ["N1"]
        result = gateway.get_notice_stocks()
        assert result == ["N1"]
        mock_client._cache_set.assert_called_once_with("notice", _NOTICE_TTL_S, ["N1"])

    def test_credit_calls_api(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        contracts = [MagicMock()]
        mock_client.api.credit_enquires.return_value = [{"c": 1}]
        result = gateway.get_credit_enquiries(contracts)
        assert result == [{"c": 1}]
        mock_client.api.credit_enquires.assert_called_once_with(contracts)
        mock_client._cache_set.assert_called_once_with("credit_enquiries", _CREDIT_TTL_S, [{"c": 1}])

    def test_short_stock_calls_api(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        contracts = [MagicMock()]
        mock_client.api.short_stock_sources.return_value = [{"s": 1}]
        result = gateway.get_short_stock_sources(contracts)
        assert result == [{"s": 1}]
        mock_client.api.short_stock_sources.assert_called_once_with(contracts)
        mock_client._cache_set.assert_called_once_with("short_stock_sources", _SHORT_STOCK_TTL_S, [{"s": 1}])


# ── Rate limit rejection returns fallback ─────────────────────────

class TestRateLimit:
    def test_punish_rate_limited(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._rate_limit_api.return_value = False
        result = gateway.get_punish_stocks()
        assert result is None
        mock_client.api.punish.assert_not_called()

    def test_notice_rate_limited(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._rate_limit_api.return_value = False
        result = gateway.get_notice_stocks()
        assert result is None
        mock_client.api.notice.assert_not_called()

    def test_credit_rate_limited(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._rate_limit_api.return_value = False
        result = gateway.get_credit_enquiries([MagicMock()])
        assert result == []
        mock_client.api.credit_enquires.assert_not_called()

    def test_short_stock_rate_limited(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client._rate_limit_api.return_value = False
        result = gateway.get_short_stock_sources([MagicMock()])
        assert result == []
        mock_client.api.short_stock_sources.assert_not_called()


# ── API error returns fallback gracefully ─────────────────────────

class TestApiError:
    def test_punish_api_error(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.punish.side_effect = RuntimeError("network")
        result = gateway.get_punish_stocks()
        assert result is None
        mock_client._record_api_latency.assert_called_once()
        args = mock_client._record_api_latency.call_args
        assert args[1]["ok"] is False

    def test_notice_api_error(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.notice.side_effect = RuntimeError("timeout")
        result = gateway.get_notice_stocks()
        assert result is None

    def test_credit_api_error(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.credit_enquires.side_effect = RuntimeError("err")
        result = gateway.get_credit_enquiries([MagicMock()])
        assert result == []

    def test_short_stock_api_error(self, gateway: RiskScreeningGateway, mock_client: MagicMock) -> None:
        mock_client.api.short_stock_sources.side_effect = RuntimeError("err")
        result = gateway.get_short_stock_sources([MagicMock()])
        assert result == []


# ── Long cache TTL values ─────────────────────────────────────────

class TestCacheTTL:
    def test_punish_ttl_is_300(self) -> None:
        assert _PUNISH_TTL_S == 300.0

    def test_notice_ttl_is_300(self) -> None:
        assert _NOTICE_TTL_S == 300.0

    def test_credit_ttl_is_60(self) -> None:
        assert _CREDIT_TTL_S == 60.0

    def test_short_stock_ttl_is_60(self) -> None:
        assert _SHORT_STOCK_TTL_S == 60.0

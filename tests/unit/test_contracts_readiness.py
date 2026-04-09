"""Tests for the contracts readiness gate.

Covers:
- ShioajiClient._ensure_contracts() returns bool
- ShioajiClient.contracts_ready property
- session_runtime sets _contracts_ready and logs appropriately
- OrderGateway.place_order() rejects when contracts not ready
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(config_path: str) -> Any:
    """Return a ShioajiClient with a mocked API."""
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    client = ShioajiClient(config_path=config_path)
    client.metrics = MagicMock()
    return client


@pytest.fixture()
def symbols_config(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(yaml.dump({"symbols": [{"code": "2330", "exchange": "TSE"}]}))
    return str(cfg)


@pytest.fixture()
def client_no_contracts(symbols_config):
    """ShioajiClient whose api has NO Contracts attribute."""
    c = _make_client(symbols_config)
    mock_api = MagicMock(spec=[])  # spec=[] means no attributes
    mock_api.fetch_contracts = MagicMock()
    c.api = mock_api
    return c


@pytest.fixture()
def client_with_contracts(symbols_config):
    """ShioajiClient whose api already has Contracts attribute."""
    c = _make_client(symbols_config)
    mock_api = MagicMock()
    mock_api.Contracts = MagicMock()  # Contracts exists
    c.api = mock_api
    return c


# ---------------------------------------------------------------------------
# contracts_ready property
# ---------------------------------------------------------------------------


class TestContractsReadyProperty:
    def test_returns_false_when_api_is_none(self, symbols_config):
        c = _make_client(symbols_config)
        c.api = None
        assert c.contracts_ready is False

    def test_returns_false_when_contracts_attribute_missing(self, client_no_contracts):
        assert client_no_contracts.contracts_ready is False

    def test_returns_true_when_contracts_attribute_present(self, client_with_contracts):
        assert client_with_contracts.contracts_ready is True


# ---------------------------------------------------------------------------
# _ensure_contracts return value
# ---------------------------------------------------------------------------


class TestEnsureContracts:
    def test_returns_false_when_api_is_none(self, symbols_config):
        c = _make_client(symbols_config)
        c.api = None
        result = c._ensure_contracts()
        assert result is False

    def test_returns_false_when_fetch_contracts_not_available(self, symbols_config):
        """api exists but has no fetch_contracts method."""
        c = _make_client(symbols_config)
        # api has Contracts already but no fetch_contracts — edge case
        # Should still return contracts_ready (True here)
        mock_api = MagicMock(spec=["Contracts"])
        mock_api.Contracts = MagicMock()
        c.api = mock_api
        result = c._ensure_contracts()
        assert result is True

    def test_returns_true_when_fetch_succeeds_and_contracts_loaded(self, client_no_contracts):
        """After a successful fetch_contracts call, Contracts is available."""

        # Simulate the SDK setting Contracts after fetch
        def _side_effect(**kwargs):
            client_no_contracts.api.Contracts = MagicMock()

        client_no_contracts.api.fetch_contracts = MagicMock(side_effect=_side_effect)
        result = client_no_contracts._ensure_contracts()
        assert result is True

    def test_returns_false_when_fetch_raises_and_contracts_not_loaded(self, client_no_contracts):
        """fetch_contracts raises; Contracts still not available → False."""
        client_no_contracts.api.fetch_contracts = MagicMock(side_effect=RuntimeError("timeout"))
        result = client_no_contracts._ensure_contracts()
        assert result is False

    def test_returns_false_when_fetch_succeeds_but_contracts_still_missing(self, client_no_contracts):
        """fetch_contracts call succeeds but SDK didn't populate Contracts."""
        client_no_contracts.api.fetch_contracts = MagicMock()  # does nothing
        result = client_no_contracts._ensure_contracts()
        assert result is False

    def test_records_latency_on_success(self, client_no_contracts):
        def _side_effect(**kwargs):
            client_no_contracts.api.Contracts = MagicMock()

        client_no_contracts.api.fetch_contracts = MagicMock(side_effect=_side_effect)
        client_no_contracts._record_api_latency = MagicMock()
        client_no_contracts._ensure_contracts()
        # Verify latency was recorded with the right operation name and ok=True.
        # The timestamp argument is a perf_counter_ns value — just verify it's an int.
        assert client_no_contracts._record_api_latency.call_count == 1
        call_args = client_no_contracts._record_api_latency.call_args
        assert call_args[0][0] == "fetch_contracts"
        assert isinstance(call_args[0][1], int)
        assert call_args[1]["ok"] is True

    def test_records_latency_on_failure(self, client_no_contracts):
        client_no_contracts.api.fetch_contracts = MagicMock(side_effect=Exception("boom"))
        client_no_contracts._record_api_latency = MagicMock()
        client_no_contracts._ensure_contracts()
        assert client_no_contracts._record_api_latency.call_count == 1
        call_args = client_no_contracts._record_api_latency.call_args
        assert call_args[0][0] == "fetch_contracts"
        assert isinstance(call_args[0][1], int)
        assert call_args[1]["ok"] is False


# ---------------------------------------------------------------------------
# session_runtime contracts logging
# ---------------------------------------------------------------------------


def _make_session_mock_client(contracts_ready: bool):
    """Build a mock client for session_runtime tests."""
    c = MagicMock()
    c.api = MagicMock()
    c.logged_in = False
    c._contracts_ready = False
    c.ca_active = False
    c.activate_ca = False
    c.ca_path = ""
    c.fetch_contract = True
    c.contracts_timeout = 30
    c.subscribe_trade = True
    c._login_retry_max = 1
    c._login_timeout_s = 30.0
    c._last_login_error = None
    c._last_reconnect_error = None
    c._reconnect_backoff_s = 30.0
    c._last_session_refresh_ts = 0.0
    c._session_refresh_running = False
    c._session_refresh_interval_s = 3600
    c._session_refresh_check_interval_s = 60
    c._session_refresh_holiday_aware = False
    c.tick_callback = None
    c.metrics = MagicMock()
    c._safe_call_with_timeout = MagicMock(return_value=(True, None, None, False))
    c._record_api_latency = MagicMock()
    c._ensure_session_lock = MagicMock()
    c._release_session_lock = MagicMock()
    c._ensure_contracts = MagicMock(return_value=contracts_ready)
    c.contracts_ready = contracts_ready
    c._sanitize_metric_label = MagicMock(return_value="unknown")
    c.reconnect = MagicMock(return_value=True)
    return c


class TestSessionRuntimeContractsFlag:
    def test_sets_contracts_ready_true_when_contracts_available(self):
        from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime

        c = _make_session_mock_client(contracts_ready=True)
        rt = SessionRuntime(c)
        with patch.dict(os.environ, {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}):
            result = rt.login_with_retry()

        assert result is True
        assert c._contracts_ready is True

    def test_sets_contracts_ready_false_when_contracts_missing(self):
        from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime

        c = _make_session_mock_client(contracts_ready=False)
        rt = SessionRuntime(c)
        with patch.dict(os.environ, {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}):
            result = rt.login_with_retry()

        assert result is True
        # Login still succeeds even without contracts
        assert c.logged_in is True
        assert c._contracts_ready is False


# ---------------------------------------------------------------------------
# OrderGateway.place_order — contracts_ready guard
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_sj_module():
    with patch("hft_platform.feed_adapter.shioaji_client.sj") as sj:
        sj.constant.Action.Buy = "Buy"
        sj.constant.Action.Sell = "Sell"
        sj.constant.StockPriceType.LMT = "LMT"
        sj.constant.OrderType.ROD = "ROD"
        sj.constant.OrderType.IOC = "IOC"
        sj.constant.OrderType.FOK = "FOK"
        sj.Order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        yield sj


@pytest.fixture()
def gateway_no_contracts(symbols_config, mock_sj_module):
    """OrderGateway whose client has api but NO Contracts."""
    from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    client = ShioajiClient(config_path=symbols_config)
    mock_api = MagicMock(spec=[])  # no Contracts attribute
    client.api = mock_api
    client.metrics = MagicMock()
    yield OrderGateway(client)
    client.close()


@pytest.fixture()
def gateway_with_contracts(symbols_config, mock_sj_module):
    """OrderGateway whose client has api AND Contracts."""
    from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    client = ShioajiClient(config_path=symbols_config)
    mock_api = MagicMock()
    mock_api.Contracts = MagicMock()
    mock_contract = MagicMock()
    mock_contract.code = "2330"
    mock_api.Contracts.Stocks.TSE = {"2330": mock_contract}
    client.api = mock_api
    client.metrics = MagicMock()
    yield OrderGateway(client)
    client.close()


class TestPlaceOrderContractsGate:
    def test_raises_runtime_error_when_contracts_not_ready(self, gateway_no_contracts):
        with pytest.raises(RuntimeError, match="Contracts not loaded"):
            gateway_no_contracts.place_order(
                contract_code="2330",
                exchange="TSE",
                action="Buy",
                price=600000,
                qty=1,
                order_type="LMT",
                tif="ROD",
            )

    def test_proceeds_when_contracts_are_ready(self, gateway_with_contracts):
        api = gateway_with_contracts._client.api
        api.place_order.return_value = {"status": "ok"}

        result = gateway_with_contracts.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=600000,
            qty=1,
            order_type="LMT",
            tif="ROD",
        )

        api.place_order.assert_called_once()
        assert result == {"status": "ok"}

    def test_no_contracts_check_when_api_is_none(self, symbols_config, mock_sj_module):
        """When api is None, mock mode is used — no RuntimeError raised."""
        from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
        from hft_platform.feed_adapter.shioaji_client import ShioajiClient

        client = ShioajiClient(config_path=symbols_config)
        client.api = None  # simulates SDK-not-installed path
        client.metrics = MagicMock()
        gw = OrderGateway(client)

        result = gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=600000,
            qty=1,
            order_type="LMT",
            tif="ROD",
        )

        assert "seq_no" in result
        client.close()

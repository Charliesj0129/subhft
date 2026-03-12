"""Tests for HistoricalGateway (Shioaji ticks/kbars)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji.historical_gateway import (
    HistoricalGateway,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    *,
    api: MagicMock | None = None,
    logged_in: bool = True,
    rate_limit_ok: bool = True,
    contract: object | None = "mock_contract",
) -> MagicMock:
    """Build a minimal mock ShioajiClient."""
    client = MagicMock()
    client.api = api or MagicMock()
    client.logged_in = logged_in
    client._rate_limit_api = MagicMock(return_value=rate_limit_ok)
    client._get_contract = MagicMock(return_value=contract)
    client._record_api_latency = MagicMock()
    return client


def _make_sdk() -> SimpleNamespace:
    """Build a minimal mock Shioaji SDK namespace."""
    ticks_qt = SimpleNamespace(AllDay="AllDay", RangeTime="RangeTime", LastCount="LastCount")
    constant = SimpleNamespace(TicksQueryType=ticks_qt)
    return SimpleNamespace(constant=constant)


SDK_PATCH = "hft_platform.feed_adapter.shioaji.historical_gateway.HistoricalGateway._sdk"


# ---------------------------------------------------------------------------
# get_ticks — happy path
# ---------------------------------------------------------------------------


class TestGetTicks:
    def test_returns_ticks_all_day(self) -> None:
        client = _make_client()
        client.api.ticks.return_value = {"ts": [1, 2], "close": [100, 101]}
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            result = gw.get_ticks("2330", "TSE", "2026-03-10")

        assert result == {"ts": [1, 2], "close": [100, 101]}
        client.api.ticks.assert_called_once()
        call_kwargs = client.api.ticks.call_args.kwargs
        assert call_kwargs["contract"] == "mock_contract"
        assert call_kwargs["date"] == "2026-03-10"
        assert call_kwargs["query_type"] == "AllDay"
        assert call_kwargs["timeout"] == 30000
        client._record_api_latency.assert_called_once()
        assert client._record_api_latency.call_args.args[0] == "ticks"
        assert client._record_api_latency.call_args.kwargs.get("ok") is True

    def test_passes_range_time_params(self) -> None:
        client = _make_client()
        client.api.ticks.return_value = []
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            gw.get_ticks(
                "2330",
                "TSE",
                "2026-03-10",
                query_type="RangeTime",
                time_start="09:00:00",
                time_end="10:00:00",
            )

        kwargs = client.api.ticks.call_args.kwargs
        assert kwargs["query_type"] == "RangeTime"
        assert kwargs["time_start"] == "09:00:00"
        assert kwargs["time_end"] == "10:00:00"

    def test_passes_last_count_param(self) -> None:
        client = _make_client()
        client.api.ticks.return_value = []
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            gw.get_ticks(
                "2330",
                "TSE",
                "2026-03-10",
                query_type="LastCount",
                last_cnt=100,
            )

        kwargs = client.api.ticks.call_args.kwargs
        assert kwargs["query_type"] == "LastCount"
        assert kwargs["last_cnt"] == 100

    def test_custom_timeout(self) -> None:
        client = _make_client()
        client.api.ticks.return_value = []
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            gw.get_ticks("2330", "TSE", "2026-03-10", timeout=60000)

        kwargs = client.api.ticks.call_args.kwargs
        assert kwargs["timeout"] == 60000

    def test_product_type_forwarded(self) -> None:
        client = _make_client()
        client.api.ticks.return_value = []
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            gw.get_ticks("TXFJ4", "TFE", "2026-03-10", product_type="future")

        client._get_contract.assert_called_once_with(
            "TFE",
            "TXFJ4",
            product_type="future",
            allow_synthetic=False,
        )


# ---------------------------------------------------------------------------
# get_ticks — error paths
# ---------------------------------------------------------------------------


class TestGetTicksErrors:
    def test_returns_none_when_api_missing(self) -> None:
        client = _make_client(api=None)
        client.api = None
        gw = HistoricalGateway(client)

        result = gw.get_ticks("2330", "TSE", "2026-03-10")
        assert result is None

    def test_returns_none_when_not_logged_in(self) -> None:
        client = _make_client(logged_in=False)
        gw = HistoricalGateway(client)

        result = gw.get_ticks("2330", "TSE", "2026-03-10")
        assert result is None

    def test_returns_none_on_rate_limit(self) -> None:
        client = _make_client(rate_limit_ok=False)
        gw = HistoricalGateway(client)

        result = gw.get_ticks("2330", "TSE", "2026-03-10")
        assert result is None

    def test_raises_on_contract_not_found(self) -> None:
        client = _make_client(contract=None)
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            with pytest.raises(ValueError, match="not found"):
                gw.get_ticks("INVALID", "TSE", "2026-03-10")

    def test_raises_on_unknown_query_type(self) -> None:
        client = _make_client()
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            with pytest.raises(ValueError, match="Unknown query_type"):
                gw.get_ticks("2330", "TSE", "2026-03-10", query_type="BadType")

    def test_raises_on_sdk_unavailable(self) -> None:
        client = _make_client()
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=None):
            with pytest.raises(RuntimeError, match="SDK unavailable"):
                gw.get_ticks("2330", "TSE", "2026-03-10")

    def test_records_latency_on_api_error(self) -> None:
        client = _make_client()
        client.api.ticks.side_effect = RuntimeError("network")
        gw = HistoricalGateway(client)

        with patch(SDK_PATCH, return_value=_make_sdk()):
            with pytest.raises(RuntimeError, match="network"):
                gw.get_ticks("2330", "TSE", "2026-03-10")

        client._record_api_latency.assert_called_once()
        assert client._record_api_latency.call_args.kwargs.get("ok") is False


# ---------------------------------------------------------------------------
# get_kbars — happy path
# ---------------------------------------------------------------------------


class TestGetKbars:
    def test_returns_kbars(self) -> None:
        client = _make_client()
        client.api.kbars.return_value = {"ts": [1], "Open": [100]}
        gw = HistoricalGateway(client)

        result = gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10")

        assert result == {"ts": [1], "Open": [100]}
        client.api.kbars.assert_called_once_with(
            contract="mock_contract",
            start="2026-03-01",
            end="2026-03-10",
            timeout=30000,
        )
        client._record_api_latency.assert_called_once()
        assert client._record_api_latency.call_args.args[0] == "kbars"
        assert client._record_api_latency.call_args.kwargs.get("ok") is True

    def test_custom_timeout(self) -> None:
        client = _make_client()
        client.api.kbars.return_value = []
        gw = HistoricalGateway(client)

        gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10", timeout=60000)

        kwargs = client.api.kbars.call_args.kwargs
        assert kwargs["timeout"] == 60000

    def test_product_type_forwarded(self) -> None:
        client = _make_client()
        client.api.kbars.return_value = []
        gw = HistoricalGateway(client)

        gw.get_kbars("TXFJ4", "TFE", "2026-03-01", "2026-03-10", product_type="future")

        client._get_contract.assert_called_once_with(
            "TFE",
            "TXFJ4",
            product_type="future",
            allow_synthetic=False,
        )


# ---------------------------------------------------------------------------
# get_kbars — error paths
# ---------------------------------------------------------------------------


class TestGetKbarsErrors:
    def test_returns_none_when_api_missing(self) -> None:
        client = _make_client(api=None)
        client.api = None
        gw = HistoricalGateway(client)

        result = gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10")
        assert result is None

    def test_returns_none_when_not_logged_in(self) -> None:
        client = _make_client(logged_in=False)
        gw = HistoricalGateway(client)

        result = gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10")
        assert result is None

    def test_returns_none_on_rate_limit(self) -> None:
        client = _make_client(rate_limit_ok=False)
        gw = HistoricalGateway(client)

        result = gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10")
        assert result is None

    def test_raises_on_contract_not_found(self) -> None:
        client = _make_client(contract=None)
        gw = HistoricalGateway(client)

        with pytest.raises(ValueError, match="not found"):
            gw.get_kbars("INVALID", "TSE", "2026-03-01", "2026-03-10")

    def test_records_latency_on_api_error(self) -> None:
        client = _make_client()
        client.api.kbars.side_effect = RuntimeError("timeout")
        gw = HistoricalGateway(client)

        with pytest.raises(RuntimeError, match="timeout"):
            gw.get_kbars("2330", "TSE", "2026-03-01", "2026-03-10")

        client._record_api_latency.assert_called_once()
        assert client._record_api_latency.call_args.kwargs.get("ok") is False


# ---------------------------------------------------------------------------
# _resolve_query_type
# ---------------------------------------------------------------------------


class TestResolveQueryType:
    def test_all_valid_types(self) -> None:
        gw = HistoricalGateway(_make_client())
        sdk = _make_sdk()

        with patch(SDK_PATCH, return_value=sdk):
            assert gw._resolve_query_type("AllDay") == "AllDay"
            assert gw._resolve_query_type("RangeTime") == "RangeTime"
            assert gw._resolve_query_type("LastCount") == "LastCount"

    def test_missing_ticks_query_type_constant(self) -> None:
        gw = HistoricalGateway(_make_client())
        sdk = SimpleNamespace(constant=SimpleNamespace())  # no TicksQueryType

        with patch(SDK_PATCH, return_value=sdk):
            with pytest.raises(RuntimeError, match="TicksQueryType"):
                gw._resolve_query_type("AllDay")

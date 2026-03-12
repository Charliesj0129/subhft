"""Tests for DataGateway (historical ticks, kbars, snapshots)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.data_gateway import DataGateway

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    *,
    logged_in: bool = True,
    has_api: bool = True,
    rate_limit_ok: bool = True,
    symbols: list | None = None,
) -> MagicMock:
    """Return a mock ShioajiClient with sensible defaults."""
    client = MagicMock()
    client.api = MagicMock() if has_api else None
    client.logged_in = logged_in
    client._rate_limit_api = MagicMock(return_value=rate_limit_ok)
    client._record_api_latency = MagicMock()
    client.symbols = symbols or []
    return client


# ===================================================================
# get_ticks
# ===================================================================


class TestGetTicks:
    def test_calls_api_with_required_params(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        contract = MagicMock()

        gw.get_ticks(contract, date="2026-03-10")

        client.api.ticks.assert_called_once()
        call_kwargs = client.api.ticks.call_args[1]
        assert call_kwargs["contract"] is contract
        assert call_kwargs["date"] == "2026-03-10"
        assert call_kwargs["timeout"] == 30000
        # Optional params should NOT be present
        assert "time_start" not in call_kwargs
        assert "time_end" not in call_kwargs
        assert "last_cnt" not in call_kwargs

    def test_passes_optional_params_when_provided(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        contract = MagicMock()

        gw.get_ticks(
            contract,
            date="2026-03-10",
            time_start="09:00:00",
            time_end="13:30:00",
            last_cnt=100,
        )

        call_kwargs = client.api.ticks.call_args[1]
        assert call_kwargs["time_start"] == "09:00:00"
        assert call_kwargs["time_end"] == "13:30:00"
        assert call_kwargs["last_cnt"] == 100

    def test_returns_none_when_api_unavailable(self) -> None:
        client = _make_client(has_api=False)
        gw = DataGateway(client)
        result = gw.get_ticks(MagicMock(), date="2026-03-10")
        assert result is None

    def test_returns_none_when_not_logged_in(self) -> None:
        client = _make_client(logged_in=False)
        gw = DataGateway(client)
        result = gw.get_ticks(MagicMock(), date="2026-03-10")
        assert result is None

    def test_returns_none_when_rate_limited(self) -> None:
        client = _make_client(rate_limit_ok=False)
        gw = DataGateway(client)
        result = gw.get_ticks(MagicMock(), date="2026-03-10")
        assert result is None
        client._rate_limit_api.assert_called_once_with("ticks")

    def test_records_latency_on_success(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        gw.get_ticks(MagicMock(), date="2026-03-10")
        client._record_api_latency.assert_called_once()
        pos, kw = client._record_api_latency.call_args
        assert pos[0] == "ticks"
        assert kw["ok"] is True

    def test_records_latency_on_failure(self) -> None:
        client = _make_client()
        client.api.ticks.side_effect = RuntimeError("timeout")
        gw = DataGateway(client)
        result = gw.get_ticks(MagicMock(), date="2026-03-10")
        assert result is None
        pos, kw = client._record_api_latency.call_args
        assert pos[0] == "ticks"
        assert kw["ok"] is False

    def test_query_type_string_mapped_to_enum(self) -> None:
        """When sj is available, the query_type string is resolved to an enum."""
        mock_enum = MagicMock()
        mock_enum.RangeTime = "RANGE_TIME_ENUM"
        mock_enum.AllDay = "ALL_DAY_ENUM"

        with patch.object(DataGateway, "_resolve_ticks_query_type", return_value="RANGE_TIME_ENUM") as mock_resolve:
            client = _make_client()
            gw = DataGateway(client)
            gw.get_ticks(MagicMock(), date="2026-03-10", query_type="RangeTime")
            mock_resolve.assert_called_once_with("RangeTime")
            call_kwargs = client.api.ticks.call_args[1]
            assert call_kwargs["query_type"] == "RANGE_TIME_ENUM"

    def test_resolve_ticks_query_type_without_sj(self) -> None:
        """When sj module is None, the string is returned as-is."""
        with patch("hft_platform.feed_adapter.shioaji.data_gateway.sj", None):
            result = DataGateway._resolve_ticks_query_type("RangeTime")
            assert result == "RangeTime"

    def test_resolve_ticks_query_type_with_sj(self) -> None:
        """When sj is available, getattr resolves the enum."""
        mock_sj = MagicMock()
        mock_sj.constant.TicksQueryType.RangeTime = "RT_ENUM"
        mock_sj.constant.TicksQueryType.AllDay = "AD_ENUM"

        with patch("hft_platform.feed_adapter.shioaji.data_gateway.sj", mock_sj):
            result = DataGateway._resolve_ticks_query_type("RangeTime")
            assert result == "RT_ENUM"

    def test_resolve_ticks_query_type_unknown_falls_back(self) -> None:
        """Unknown query_type falls back to AllDay."""
        mock_sj = MagicMock()
        # Simulate getattr miss: accessing a non-existent attr should fallback
        del mock_sj.constant.TicksQueryType.NonExistent
        mock_sj.constant.TicksQueryType.AllDay = "AD_ENUM"

        with patch("hft_platform.feed_adapter.shioaji.data_gateway.sj", mock_sj):
            result = DataGateway._resolve_ticks_query_type("NonExistent")
            assert result == "AD_ENUM"


# ===================================================================
# get_kbars
# ===================================================================


class TestGetKbars:
    def test_calls_api_with_correct_params(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        contract = MagicMock()

        gw.get_kbars(contract, start="2026-03-01", end="2026-03-10")

        client.api.kbars.assert_called_once_with(
            contract=contract,
            start="2026-03-01",
            end="2026-03-10",
            timeout=30000,
        )

    def test_custom_timeout(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        gw.get_kbars(MagicMock(), start="2026-03-01", end="2026-03-10", timeout=60000)
        assert client.api.kbars.call_args[1]["timeout"] == 60000

    def test_returns_none_when_api_unavailable(self) -> None:
        client = _make_client(has_api=False)
        gw = DataGateway(client)
        assert gw.get_kbars(MagicMock(), start="2026-03-01", end="2026-03-10") is None

    def test_returns_none_when_rate_limited(self) -> None:
        client = _make_client(rate_limit_ok=False)
        gw = DataGateway(client)
        assert gw.get_kbars(MagicMock(), start="2026-03-01", end="2026-03-10") is None
        client._rate_limit_api.assert_called_once_with("kbars")

    def test_records_latency_on_success(self) -> None:
        client = _make_client()
        gw = DataGateway(client)
        gw.get_kbars(MagicMock(), start="2026-03-01", end="2026-03-10")
        client._record_api_latency.assert_called_once()
        pos, kw = client._record_api_latency.call_args
        assert pos[0] == "kbars"
        assert kw["ok"] is True

    def test_records_latency_on_failure(self) -> None:
        client = _make_client()
        client.api.kbars.side_effect = RuntimeError("fail")
        gw = DataGateway(client)
        result = gw.get_kbars(MagicMock(), start="2026-03-01", end="2026-03-10")
        assert result is None
        pos, kw = client._record_api_latency.call_args
        assert pos[0] == "kbars"
        assert kw["ok"] is False


# ===================================================================
# get_snapshots
# ===================================================================


class TestGetSnapshots:
    def test_batches_at_500(self) -> None:
        client = _make_client()
        contracts = [MagicMock() for _ in range(1200)]
        client.api.snapshots.return_value = [{"snap": True}]

        gw = DataGateway(client)
        result = gw.get_snapshots(contracts=contracts)

        # 1200 / 500 = 3 batches
        assert client.api.snapshots.call_count == 3
        # First batch 500, second 500, third 200
        assert len(client.api.snapshots.call_args_list[0][0][0]) == 500
        assert len(client.api.snapshots.call_args_list[1][0][0]) == 500
        assert len(client.api.snapshots.call_args_list[2][0][0]) == 200
        # Results aggregated
        assert len(result) == 3  # 1 per batch

    def test_resolves_from_symbols_when_contracts_none(self) -> None:
        client = _make_client(
            symbols=[
                {"code": "2330", "exchange": "TSE", "product_type": "stock"},
                {"code": "2317", "exchange": "TSE", "product_type": "stock"},
            ]
        )
        mock_contract = MagicMock()
        client._get_contract = MagicMock(return_value=mock_contract)
        client.api.snapshots.return_value = [{"snap": True}]

        gw = DataGateway(client)
        result = gw.get_snapshots(contracts=None)

        assert client._get_contract.call_count == 2
        assert len(result) >= 1

    def test_returns_empty_when_api_unavailable(self) -> None:
        client = _make_client(has_api=False)
        gw = DataGateway(client)
        assert gw.get_snapshots() == []

    def test_returns_empty_when_not_logged_in(self) -> None:
        client = _make_client(logged_in=False)
        gw = DataGateway(client)
        assert gw.get_snapshots() == []

    def test_returns_empty_when_no_contracts(self) -> None:
        client = _make_client(symbols=[])
        gw = DataGateway(client)
        assert gw.get_snapshots(contracts=None) == []

    def test_returns_empty_when_rate_limited(self) -> None:
        client = _make_client(rate_limit_ok=False)
        gw = DataGateway(client)
        assert gw.get_snapshots(contracts=[MagicMock()]) == []
        client._rate_limit_api.assert_called_once_with("snapshots")

    def test_handles_api_error_in_batch(self) -> None:
        client = _make_client()
        # First batch succeeds, second fails
        client.api.snapshots.side_effect = [
            [{"snap": 1}],
            RuntimeError("network error"),
        ]

        gw = DataGateway(client)
        contracts = [MagicMock() for _ in range(700)]
        result = gw.get_snapshots(contracts=contracts)

        # Only first batch results returned
        assert len(result) == 1
        # Latency recorded for both
        assert client._record_api_latency.call_count == 2

    def test_skips_symbols_without_code_or_exchange(self) -> None:
        client = _make_client(
            symbols=[
                {"code": "2330", "exchange": "TSE"},
                {"code": "", "exchange": "TSE"},  # empty code
                {"code": "2317"},  # missing exchange
            ]
        )
        mock_contract = MagicMock()
        client._get_contract = MagicMock(return_value=mock_contract)
        client.api.snapshots.return_value = []

        gw = DataGateway(client)
        gw.get_snapshots(contracts=None)

        # Only the first symbol should resolve
        client._get_contract.assert_called_once()


# ===================================================================
# _resolve_symbol_contracts
# ===================================================================


class TestResolveSymbolContracts:
    def test_product_type_fallback_fields(self) -> None:
        """product_type falls back to security_type then type."""
        client = _make_client(
            symbols=[
                {"code": "A", "exchange": "TSE", "security_type": "stock"},
                {"code": "B", "exchange": "OTC", "type": "etf"},
            ]
        )
        client._get_contract = MagicMock(return_value=MagicMock())

        gw = DataGateway(client)
        result = gw._resolve_symbol_contracts()

        assert len(result) == 2
        # First call: product_type from security_type
        assert client._get_contract.call_args_list[0][1]["product_type"] == "stock"
        # Second call: product_type from type
        assert client._get_contract.call_args_list[1][1]["product_type"] == "etf"

    def test_skips_unresolvable_contracts(self) -> None:
        client = _make_client(
            symbols=[
                {"code": "GOOD", "exchange": "TSE"},
                {"code": "BAD", "exchange": "TSE"},
            ]
        )
        client._get_contract = MagicMock(side_effect=[MagicMock(), None])

        gw = DataGateway(client)
        result = gw._resolve_symbol_contracts()

        assert len(result) == 1

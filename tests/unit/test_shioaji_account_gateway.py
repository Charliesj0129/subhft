"""Unit tests for AccountGateway extended query methods."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway


def _make_client(mode: str = "live") -> MagicMock:
    """Build a minimal mock ShioajiClient for AccountGateway tests."""
    client = MagicMock()
    client.mode = mode
    client.api = MagicMock()
    client.api.stock_account = SimpleNamespace(account_id="STOCK001")
    client.api.futopt_account = SimpleNamespace(account_id="FUT001")
    client.logged_in = True
    client._cache_get = MagicMock(return_value=None)
    client._cache_set = MagicMock()
    client._rate_limit_api = MagicMock(return_value=True)
    client._record_api_latency = MagicMock()
    client._profit_cache_ttl_s = 10
    client._trading_limits_cache_ttl_s = 30
    client._settlements_cache_ttl_s = 30
    return client


# ---------------------------------------------------------------------------
# get_trading_limits
# ---------------------------------------------------------------------------


class TestGetTradingLimits:
    def test_simulation_returns_empty_dict(self) -> None:
        client = _make_client(mode="simulation")
        gw = AccountGateway(client)
        assert gw.get_trading_limits() == {}

    def test_cached_result_returned(self) -> None:
        client = _make_client()
        cached = {"limit": 5000}
        client._cache_get.return_value = cached
        gw = AccountGateway(client)
        assert gw.get_trading_limits() is cached
        client.api.trading_limits.assert_not_called()

    def test_api_called_with_stock_account(self) -> None:
        client = _make_client()
        expected = {"limit": 9999}
        client.api.trading_limits.return_value = expected
        gw = AccountGateway(client)
        result = gw.get_trading_limits()
        assert result == expected
        client.api.trading_limits.assert_called_once_with(client.api.stock_account)
        client._cache_set.assert_called_once()
        client._record_api_latency.assert_called_once()

    def test_api_called_with_explicit_account(self) -> None:
        client = _make_client()
        acct = SimpleNamespace(account_id="CUSTOM")
        client.api.trading_limits.return_value = {"limit": 1}
        gw = AccountGateway(client)
        gw.get_trading_limits(account=acct)
        client.api.trading_limits.assert_called_once_with(acct)

    def test_rate_limit_blocks(self) -> None:
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = AccountGateway(client)
        result = gw.get_trading_limits()
        assert result == {}
        client.api.trading_limits.assert_not_called()

    def test_exception_returns_empty_dict(self) -> None:
        client = _make_client()
        client.api.trading_limits.side_effect = RuntimeError("boom")
        gw = AccountGateway(client)
        result = gw.get_trading_limits()
        assert result == {}
        client._record_api_latency.assert_called_once()
        _, kwargs = client._record_api_latency.call_args
        assert kwargs.get("ok") is False or client._record_api_latency.call_args[0][2] is False


# ---------------------------------------------------------------------------
# get_settlements
# ---------------------------------------------------------------------------


class TestGetSettlements:
    def test_simulation_returns_empty_list(self) -> None:
        client = _make_client(mode="simulation")
        gw = AccountGateway(client)
        assert gw.get_settlements() == []

    def test_cached_result_returned(self) -> None:
        client = _make_client()
        cached = [{"date": "2026-03-01", "amount": 100}]
        client._cache_get.return_value = cached
        gw = AccountGateway(client)
        assert gw.get_settlements() is cached

    def test_api_called_with_stock_account(self) -> None:
        client = _make_client()
        expected = [{"date": "2026-03-01"}]
        client.api.settlements.return_value = expected
        gw = AccountGateway(client)
        result = gw.get_settlements()
        assert result == expected
        client.api.settlements.assert_called_once_with(client.api.stock_account)

    def test_api_called_with_explicit_account(self) -> None:
        client = _make_client()
        acct = SimpleNamespace(account_id="X")
        client.api.settlements.return_value = []
        gw = AccountGateway(client)
        gw.get_settlements(account=acct)
        client.api.settlements.assert_called_once_with(acct)

    def test_rate_limit_blocks(self) -> None:
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = AccountGateway(client)
        assert gw.get_settlements() == []

    def test_exception_returns_empty_list(self) -> None:
        client = _make_client()
        client.api.settlements.side_effect = RuntimeError("fail")
        gw = AccountGateway(client)
        assert gw.get_settlements() == []


# ---------------------------------------------------------------------------
# list_profit_loss_summary
# ---------------------------------------------------------------------------


class TestListProfitLossSummary:
    def test_simulation_returns_empty_list(self) -> None:
        client = _make_client(mode="simulation")
        gw = AccountGateway(client)
        assert gw.list_profit_loss_summary() == []

    def test_cached_result_returned(self) -> None:
        client = _make_client()
        cached = [{"pnl": 42}]
        client._cache_get.return_value = cached
        gw = AccountGateway(client)
        assert gw.list_profit_loss_summary(begin_date="2026-01-01", end_date="2026-03-01") is cached

    def test_api_called_with_dates(self) -> None:
        client = _make_client()
        expected = [{"pnl": 100}]
        client.api.list_profit_loss_summary.return_value = expected
        gw = AccountGateway(client)
        result = gw.list_profit_loss_summary(begin_date="2026-01-01", end_date="2026-03-01")
        assert result == expected
        client.api.list_profit_loss_summary.assert_called_once_with(
            client.api.stock_account,
            begin_date="2026-01-01",
            end_date="2026-03-01",
        )

    def test_cache_key_includes_dates(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_summary.return_value = []
        gw = AccountGateway(client)
        gw.list_profit_loss_summary(begin_date="2026-01-01", end_date="2026-03-01")
        client._cache_get.assert_called_once_with("profit_loss_summary:2026-01-01:2026-03-01")

    def test_exception_returns_empty_list(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_summary.side_effect = RuntimeError("err")
        gw = AccountGateway(client)
        assert gw.list_profit_loss_summary() == []


# ---------------------------------------------------------------------------
# list_profit_loss_detail
# ---------------------------------------------------------------------------


class TestListProfitLossDetail:
    def test_simulation_returns_empty_list(self) -> None:
        client = _make_client(mode="simulation")
        gw = AccountGateway(client)
        assert gw.list_profit_loss_detail() == []

    def test_cached_result_returned(self) -> None:
        client = _make_client()
        cached = [{"detail": 1}]
        client._cache_get.return_value = cached
        gw = AccountGateway(client)
        assert gw.list_profit_loss_detail(detail_id=5) is cached

    def test_api_called_with_detail_id(self) -> None:
        client = _make_client()
        expected = [{"trade": "abc"}]
        client.api.list_profit_loss_detail.return_value = expected
        gw = AccountGateway(client)
        result = gw.list_profit_loss_detail(detail_id=42)
        assert result == expected
        client.api.list_profit_loss_detail.assert_called_once_with(
            client.api.stock_account,
            detail_id=42,
        )

    def test_api_called_with_unit(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_detail.return_value = []
        gw = AccountGateway(client)
        gw.list_profit_loss_detail(detail_id=1, unit="Common")
        client.api.list_profit_loss_detail.assert_called_once_with(
            client.api.stock_account,
            detail_id=1,
            unit="Common",
        )

    def test_unit_none_excluded_from_kwargs(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_detail.return_value = []
        gw = AccountGateway(client)
        gw.list_profit_loss_detail(detail_id=7)
        # unit should NOT appear in the call kwargs
        call_kwargs = client.api.list_profit_loss_detail.call_args
        assert "unit" not in call_kwargs.kwargs

    def test_cache_key_includes_detail_and_unit(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_detail.return_value = []
        gw = AccountGateway(client)
        gw.list_profit_loss_detail(detail_id=3, unit="Share")
        client._cache_get.assert_called_once_with("profit_loss_detail:3:Share")

    def test_exception_returns_empty_list(self) -> None:
        client = _make_client()
        client.api.list_profit_loss_detail.side_effect = RuntimeError("x")
        gw = AccountGateway(client)
        assert gw.list_profit_loss_detail() == []


# ---------------------------------------------------------------------------
# get_positions — decoupled stock / futopt failure handling
# ---------------------------------------------------------------------------


def _make_positions_client() -> MagicMock:
    client = _make_client()
    client._positions_cache_ttl_s = 5
    return client


class TestGetPositionsDecoupled:
    """Regression: a broker-side failure on stock must not mask a healthy futopt
    query. Futopt is the trading lane (TXF/TMF/MXF); a stock-side 500 historically
    tripped the reduce-only safety latch unnecessarily.
    """

    def test_stock_fails_futopt_succeeds_returns_futopt_positions(self) -> None:
        client = _make_positions_client()
        fut_pos = [SimpleNamespace(code="TXFE6", quantity=1)]

        def _list(account):
            if account is client.api.stock_account:
                raise RuntimeError("500 Please check param.")
            return fut_pos

        client.api.list_positions.side_effect = _list
        gw = AccountGateway(client)
        result = gw.get_positions()
        assert result == fut_pos
        assert gw.last_positions_error is not None
        assert "stock" in gw.last_positions_error.lower()
        assert "500" in gw.last_positions_error

    def test_both_fail_returns_none_with_combined_error(self) -> None:
        client = _make_positions_client()
        client.api.list_positions.side_effect = RuntimeError("broker down")
        gw = AccountGateway(client)
        assert gw.get_positions() is None
        assert gw.last_positions_error is not None
        assert "stock" in gw.last_positions_error.lower()
        assert "futopt" in gw.last_positions_error.lower()
        assert "broker down" in gw.last_positions_error

    def test_both_succeed_clears_last_error(self) -> None:
        client = _make_positions_client()
        client.api.list_positions.return_value = []
        gw = AccountGateway(client)
        gw._last_positions_error = "stale"
        result = gw.get_positions()
        assert result == []
        assert gw.last_positions_error is None

    def test_last_positions_error_initial_none(self) -> None:
        gw = AccountGateway(_make_positions_client())
        assert gw.last_positions_error is None

    def test_futopt_fails_stock_succeeds_returns_stock_positions(self) -> None:
        client = _make_positions_client()
        stock_pos = [SimpleNamespace(code="2330", quantity=1000)]

        def _list(account):
            if account is client.api.futopt_account:
                raise RuntimeError("timeout")
            return stock_pos

        client.api.list_positions.side_effect = _list
        gw = AccountGateway(client)
        result = gw.get_positions()
        assert result == stock_pos
        assert gw.last_positions_error is not None
        assert "futopt" in gw.last_positions_error.lower()

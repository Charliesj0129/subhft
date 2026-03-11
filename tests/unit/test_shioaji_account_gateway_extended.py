"""Tests for the 5 new AccountGateway methods (WU2).

Each method is tested for:
- Cache hit returns cached value
- Cache miss calls API and caches result
- Rate limit rejection returns fallback
- Simulation mode returns default
- API error returns fallback
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway


def _make_gateway(mode: str = "live") -> tuple[AccountGateway, MagicMock]:
    """Create an AccountGateway with a mocked client."""
    client = MagicMock()
    client.mode = mode
    client.api = MagicMock()
    client.api.stock_account = MagicMock()
    client._account_cache_ttl_s = 30.0
    client._profit_cache_ttl_s = 60.0
    client._cache_get = MagicMock(return_value=None)
    client._cache_set = MagicMock()
    client._rate_limit_api = MagicMock(return_value=True)
    client._record_api_latency = MagicMock()
    gw = AccountGateway(client)
    return gw, client


# --- get_settlements ---


class TestGetSettlements:
    def test_sim_mode_returns_empty_list(self) -> None:
        gw, client = _make_gateway(mode="simulation")
        assert gw.get_settlements() == []
        client.api.settlements.assert_not_called()

    def test_cache_hit(self) -> None:
        gw, client = _make_gateway()
        cached = [{"date": "2026-03-10", "amount": 1000}]
        client._cache_get.return_value = cached
        assert gw.get_settlements() is cached
        client.api.settlements.assert_not_called()

    def test_cache_miss_calls_api(self) -> None:
        gw, client = _make_gateway()
        expected = [{"date": "2026-03-10", "amount": 500}]
        client.api.settlements.return_value = expected
        result = gw.get_settlements()
        assert result is expected
        client.api.settlements.assert_called_once_with(client.api.stock_account)
        client._cache_set.assert_called_once_with("settlements", client._account_cache_ttl_s, expected)

    def test_rate_limit_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client._rate_limit_api.return_value = False
        assert gw.get_settlements() == []

    def test_api_error_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client.api.settlements.side_effect = RuntimeError("timeout")
        assert gw.get_settlements() == []
        client._record_api_latency.assert_called()

    def test_explicit_account_passed_through(self) -> None:
        gw, client = _make_gateway()
        acct = MagicMock()
        client.api.settlements.return_value = []
        gw.get_settlements(account=acct)
        client.api.settlements.assert_called_once_with(acct)


# --- get_trading_limits ---


class TestGetTradingLimits:
    def test_sim_mode_returns_empty_dict(self) -> None:
        gw, client = _make_gateway(mode="simulation")
        assert gw.get_trading_limits() == {}
        client.api.trading_limits.assert_not_called()

    def test_cache_hit(self) -> None:
        gw, client = _make_gateway()
        cached = {"limit": 999}
        client._cache_get.return_value = cached
        assert gw.get_trading_limits() is cached
        client.api.trading_limits.assert_not_called()

    def test_cache_miss_calls_api(self) -> None:
        gw, client = _make_gateway()
        expected = {"limit": 500}
        client.api.trading_limits.return_value = expected
        result = gw.get_trading_limits()
        assert result is expected
        client.api.trading_limits.assert_called_once_with(account=client.api.stock_account)
        client._cache_set.assert_called_once_with("trading_limits", client._account_cache_ttl_s, expected)

    def test_rate_limit_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client._rate_limit_api.return_value = False
        assert gw.get_trading_limits() == {}

    def test_api_error_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client.api.trading_limits.side_effect = RuntimeError("timeout")
        assert gw.get_trading_limits() == {}


# --- list_profit_loss_summary ---


class TestListProfitLossSummary:
    def test_sim_mode_returns_empty_list(self) -> None:
        gw, client = _make_gateway(mode="simulation")
        assert gw.list_profit_loss_summary() == []
        client.api.list_profit_loss_summary.assert_not_called()

    def test_cache_hit(self) -> None:
        gw, client = _make_gateway()
        cached = [{"pnl": 100}]
        client._cache_get.return_value = cached
        assert gw.list_profit_loss_summary(begin_date="2026-01-01", end_date="2026-03-01") is cached
        client._cache_get.assert_called_with("profit_loss_summary:2026-01-01:2026-03-01")

    def test_cache_miss_calls_api(self) -> None:
        gw, client = _make_gateway()
        expected = [{"pnl": 200}]
        client.api.list_profit_loss_summary.return_value = expected
        result = gw.list_profit_loss_summary(begin_date="2026-01-01", end_date="2026-03-01")
        assert result is expected
        client.api.list_profit_loss_summary.assert_called_once_with(
            client.api.stock_account, begin_date="2026-01-01", end_date="2026-03-01"
        )
        client._cache_set.assert_called_once_with(
            "profit_loss_summary:2026-01-01:2026-03-01", client._profit_cache_ttl_s, expected
        )

    def test_rate_limit_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client._rate_limit_api.return_value = False
        assert gw.list_profit_loss_summary() == []

    def test_api_error_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client.api.list_profit_loss_summary.side_effect = RuntimeError("timeout")
        assert gw.list_profit_loss_summary() == []

    def test_no_account_calls_without_acct(self) -> None:
        gw, client = _make_gateway()
        del client.api.stock_account  # hasattr returns False
        expected = [{"pnl": 10}]
        client.api.list_profit_loss_summary.return_value = expected
        result = gw.list_profit_loss_summary(begin_date="2026-01-01")
        assert result is expected
        client.api.list_profit_loss_summary.assert_called_once_with(begin_date="2026-01-01", end_date=None)


# --- list_trades ---


class TestListTrades:
    def test_sim_mode_returns_empty_list(self) -> None:
        gw, client = _make_gateway(mode="simulation")
        assert gw.list_trades() == []
        client.api.list_trades.assert_not_called()

    def test_cache_hit(self) -> None:
        gw, client = _make_gateway()
        cached = [{"trade_id": "T1"}]
        client._cache_get.return_value = cached
        assert gw.list_trades() is cached
        client.api.list_trades.assert_not_called()

    def test_cache_miss_calls_api(self) -> None:
        gw, client = _make_gateway()
        expected = [{"trade_id": "T2"}]
        client.api.list_trades.return_value = expected
        result = gw.list_trades()
        assert result is expected
        client.api.list_trades.assert_called_once()
        client._cache_set.assert_called_once_with("trades", 5.0, expected)

    def test_rate_limit_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client._rate_limit_api.return_value = False
        assert gw.list_trades() == []

    def test_api_error_returns_fallback(self) -> None:
        gw, client = _make_gateway()
        client.api.list_trades.side_effect = RuntimeError("timeout")
        assert gw.list_trades() == []


# --- update_status ---


class TestUpdateStatus:
    def test_sim_mode_returns_none(self) -> None:
        gw, client = _make_gateway(mode="simulation")
        assert gw.update_status(MagicMock()) is None
        client.api.update_status.assert_not_called()

    def test_calls_api(self) -> None:
        gw, client = _make_gateway()
        trade = MagicMock()
        expected = MagicMock()
        client.api.update_status.return_value = expected
        result = gw.update_status(trade)
        assert result is expected
        client.api.update_status.assert_called_once_with(trade)
        client._record_api_latency.assert_called()

    def test_no_cache_used(self) -> None:
        gw, client = _make_gateway()
        trade = MagicMock()
        client.api.update_status.return_value = MagicMock()
        gw.update_status(trade)
        client._cache_get.assert_not_called()
        client._cache_set.assert_not_called()

    def test_rate_limit_returns_none(self) -> None:
        gw, client = _make_gateway()
        client._rate_limit_api.return_value = False
        assert gw.update_status(MagicMock()) is None

    def test_api_error_returns_none(self) -> None:
        gw, client = _make_gateway()
        client.api.update_status.side_effect = RuntimeError("timeout")
        assert gw.update_status(MagicMock()) is None

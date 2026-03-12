"""Tests for tiered rate limiter routing in ShioajiClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji_client import ShioajiClient


@pytest.fixture()
def client() -> ShioajiClient:
    """Create a ShioajiClient with mocked dependencies for rate limiter testing."""
    with patch("hft_platform.feed_adapter.shioaji_client.sj", create=True):
        c = ShioajiClient.__new__(ShioajiClient)
        # Minimal init for rate limiter attributes only
        from hft_platform.order.rate_limiter import RateLimiter

        c._api_rate_limiter = RateLimiter(soft_cap=20, hard_cap=25, window_s=5)
        c._order_rate_limiter = RateLimiter(soft_cap=200, hard_cap=250, window_s=10)
        c._quote_query_rate_limiter = RateLimiter(soft_cap=40, hard_cap=50, window_s=5)
        c._account_rate_limiter = RateLimiter(soft_cap=20, hard_cap=25, window_s=5)
        return c


class TestTieredRateLimiterRouting:
    """Verify that _rate_limit_api routes operations to the correct limiter."""

    @pytest.mark.parametrize(
        "op",
        ["place_order", "cancel_order", "update_order", "update_price", "update_qty"],
    )
    def test_order_ops_use_order_limiter(self, client: ShioajiClient, op: str) -> None:
        client._order_rate_limiter = MagicMock()
        client._order_rate_limiter.check.return_value = True
        assert client._rate_limit_api(op) is True
        client._order_rate_limiter.check.assert_called_once()
        client._order_rate_limiter.record.assert_called_once()

    @pytest.mark.parametrize(
        "op",
        ["snapshots", "ticks", "kbars", "scanners", "credit_enquires"],
    )
    def test_quote_query_ops_use_quote_limiter(self, client: ShioajiClient, op: str) -> None:
        client._quote_query_rate_limiter = MagicMock()
        client._quote_query_rate_limiter.check.return_value = True
        assert client._rate_limit_api(op) is True
        client._quote_query_rate_limiter.check.assert_called_once()
        client._quote_query_rate_limiter.record.assert_called_once()

    @pytest.mark.parametrize(
        "op",
        [
            "usage",
            "positions",
            "account_balance",
            "margin",
            "position_detail",
            "profit_loss",
            "trading_limits",
            "settlements",
        ],
    )
    def test_account_ops_use_account_limiter(self, client: ShioajiClient, op: str) -> None:
        client._account_rate_limiter = MagicMock()
        client._account_rate_limiter.check.return_value = True
        assert client._rate_limit_api(op) is True
        client._account_rate_limiter.check.assert_called_once()
        client._account_rate_limiter.record.assert_called_once()

    def test_unknown_op_uses_fallback_limiter(self, client: ShioajiClient) -> None:
        client._api_rate_limiter = MagicMock()
        client._api_rate_limiter.check.return_value = True
        assert client._rate_limit_api("some_unknown_op") is True
        client._api_rate_limiter.check.assert_called_once()
        client._api_rate_limiter.record.assert_called_once()

    def test_rate_limit_hit_returns_false(self, client: ShioajiClient) -> None:
        client._order_rate_limiter = MagicMock()
        client._order_rate_limiter.check.return_value = False
        assert client._rate_limit_api("place_order") is False
        client._order_rate_limiter.record.assert_not_called()

    def test_limiter_isolation(self, client: ShioajiClient) -> None:
        """Order ops should not affect account limiter and vice versa."""
        # Exhaust account limiter
        client._account_rate_limiter = MagicMock()
        client._account_rate_limiter.check.return_value = False

        # Order limiter should still work
        client._order_rate_limiter = MagicMock()
        client._order_rate_limiter.check.return_value = True

        assert client._rate_limit_api("positions") is False
        assert client._rate_limit_api("place_order") is True


class TestTieredRateLimiterDefaults:
    """Verify the default capacity values for each tiered limiter."""

    def test_order_limiter_defaults(self, client: ShioajiClient) -> None:
        assert client._order_rate_limiter.soft_cap == 200
        assert client._order_rate_limiter.hard_cap == 250
        assert client._order_rate_limiter.window_s == 10

    def test_quote_query_limiter_defaults(self, client: ShioajiClient) -> None:
        assert client._quote_query_rate_limiter.soft_cap == 40
        assert client._quote_query_rate_limiter.hard_cap == 50
        assert client._quote_query_rate_limiter.window_s == 5

    def test_account_limiter_defaults(self, client: ShioajiClient) -> None:
        assert client._account_rate_limiter.soft_cap == 20
        assert client._account_rate_limiter.hard_cap == 25
        assert client._account_rate_limiter.window_s == 5

    def test_fallback_limiter_preserved(self, client: ShioajiClient) -> None:
        assert client._api_rate_limiter.soft_cap == 20
        assert client._api_rate_limiter.hard_cap == 25
        assert client._api_rate_limiter.window_s == 5

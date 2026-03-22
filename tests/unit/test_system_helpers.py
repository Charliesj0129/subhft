"""Tests for HFTSystem static helper methods.

These are pure static methods that do not require instantiating HFTSystem.
"""

from unittest.mock import MagicMock

import pytest

from hft_platform.services.system import HFTSystem

# ---------------------------------------------------------------------------
# _get_max_feed_gap_s
# ---------------------------------------------------------------------------


class TestGetMaxFeedGapS:
    def test_no_method_returns_zero(self) -> None:
        md_service = MagicMock(spec=[])
        assert HFTSystem._get_max_feed_gap_s(md_service) == 0.0

    def test_within_reconnect_window_returns_gap(self) -> None:
        md_service = MagicMock()
        md_service.get_max_feed_gap_s.return_value = 5.5
        md_service.within_reconnect_window.return_value = True
        assert HFTSystem._get_max_feed_gap_s(md_service) == 5.5

    def test_not_within_reconnect_window_returns_zero(self) -> None:
        md_service = MagicMock()
        md_service.get_max_feed_gap_s.return_value = 5.5
        md_service.within_reconnect_window.return_value = False
        assert HFTSystem._get_max_feed_gap_s(md_service) == 0.0

    def test_no_within_reconnect_window_method_returns_gap(self) -> None:
        md_service = MagicMock(spec=["get_max_feed_gap_s"])
        md_service.get_max_feed_gap_s.return_value = 3.2
        assert HFTSystem._get_max_feed_gap_s(md_service) == 3.2


# ---------------------------------------------------------------------------
# _get_drawdown_pct
# ---------------------------------------------------------------------------


class TestGetDrawdownPct:
    def test_has_get_drawdown_pct(self) -> None:
        store = MagicMock()
        store.get_drawdown_pct.return_value = -0.05
        assert HFTSystem._get_drawdown_pct(store, {}) == -0.05

    def test_negative_total_pnl_computes_from_capital(self) -> None:
        store = MagicMock(spec=["total_pnl"])
        store.total_pnl = -500_000
        settings = {"base_capital": 10_000_000}
        assert HFTSystem._get_drawdown_pct(store, settings) == pytest.approx(-0.05)

    def test_positive_total_pnl_returns_zero(self) -> None:
        store = MagicMock(spec=["total_pnl"])
        store.total_pnl = 100_000
        assert HFTSystem._get_drawdown_pct(store, {}) == 0.0

    def test_zero_base_capital_returns_zero(self) -> None:
        store = MagicMock(spec=["total_pnl"])
        store.total_pnl = -500_000
        settings = {"base_capital": 0}
        assert HFTSystem._get_drawdown_pct(store, settings) == 0.0

    def test_none_total_pnl_returns_zero(self) -> None:
        store = MagicMock(spec=[])
        assert HFTSystem._get_drawdown_pct(store, {}) == 0.0


# ---------------------------------------------------------------------------
# _get_feed_gaps_by_symbol
# ---------------------------------------------------------------------------


class TestGetFeedGapsBySymbol:
    def test_no_method_returns_empty_dict(self) -> None:
        md_service = MagicMock(spec=[])
        assert HFTSystem._get_feed_gaps_by_symbol(md_service) == {}

    def test_has_method_returns_result(self) -> None:
        md_service = MagicMock()
        expected = {"2330": 1.2, "2317": 0.8}
        md_service.get_feed_gaps_by_symbol.return_value = expected
        assert HFTSystem._get_feed_gaps_by_symbol(md_service) == expected


# ---------------------------------------------------------------------------
# _set_service_running
# ---------------------------------------------------------------------------


class TestSetServiceRunning:
    def test_sets_running_attribute(self) -> None:
        service = MagicMock()
        service.running = False
        HFTSystem._set_service_running(service, True)
        assert service.running is True

    def test_no_running_attribute_no_error(self) -> None:
        service = MagicMock(spec=[])
        HFTSystem._set_service_running(service, True)  # should not raise
        assert not hasattr(service, "running")

"""Tests for the stale-instrument gate in the Shioaji feed adapter.

Critical correctness requirement (L2 plan, loop_v1 convergence):
  - delivery_date < today  → StaleInstrumentError (contract is expired)
  - delivery_date == today → OK (rollover day: same-day expiry must not block trading)
  - delivery_date > today  → OK (active contract)
"""

from __future__ import annotations

import datetime
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.contracts_runtime import (
    StaleInstrumentError,
    assert_no_stale_subscriptions,
    assert_not_expired,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_contract(code: str, delivery_date: date) -> SimpleNamespace:
    """Return a minimal fake contract object with code and delivery_date."""
    return SimpleNamespace(code=code, delivery_date=delivery_date)


# ---------------------------------------------------------------------------
# Unit tests for assert_not_expired
# ---------------------------------------------------------------------------


class TestAssertNotExpired:
    """Tests for the assert_not_expired helper function."""

    def test_blocks_when_delivery_date_strictly_before_today(self) -> None:
        """A contract expired yesterday must raise StaleInstrumentError."""
        today = date(2026, 5, 5)
        yesterday = today - datetime.timedelta(days=1)
        contract = _make_contract("TXFD6", yesterday)

        with pytest.raises(StaleInstrumentError) as exc_info:
            assert_not_expired(contract, today=today)

        err = exc_info.value
        assert err.code == "TXFD6"
        assert err.delivery_date == yesterday

    def test_passes_on_rollover_day(self) -> None:
        """Same-day expiry (rollover day) must NOT raise — trading must continue."""
        today = date(2026, 5, 5)
        contract = _make_contract("TMFE6", today)

        # Critical rollover-day-safe requirement: returns None on success.
        assert assert_not_expired(contract, today=today) is None

    def test_passes_for_future_delivery(self) -> None:
        """A contract expiring 30 days from now must not raise."""
        today = date(2026, 5, 5)
        future = today + datetime.timedelta(days=30)
        contract = _make_contract("TMFR1", future)

        assert assert_not_expired(contract, today=today) is None

    def test_blocks_one_day_before_today(self) -> None:
        """Boundary: exactly one day before today triggers the gate."""
        today = date(2026, 5, 20)
        one_day_ago = date(2026, 5, 19)
        contract = _make_contract("TXFR1", one_day_ago)

        with pytest.raises(StaleInstrumentError):
            assert_not_expired(contract, today=today)

    def test_error_preserves_code_and_date(self) -> None:
        """StaleInstrumentError must carry code and delivery_date fields."""
        today = date(2026, 5, 5)
        expired = date(2026, 4, 15)
        contract = _make_contract("TXO15000C6", expired)

        with pytest.raises(StaleInstrumentError) as exc_info:
            assert_not_expired(contract, today=today)

        err = exc_info.value
        assert err.code == "TXO15000C6"
        assert err.delivery_date == expired


# ---------------------------------------------------------------------------
# StaleInstrumentError smoke test
# ---------------------------------------------------------------------------


class TestStaleInstrumentError:
    """StaleInstrumentError must be a proper Exception subclass."""

    def test_is_exception(self) -> None:
        err = StaleInstrumentError(code="TMFE6", delivery_date=date(2026, 4, 15))
        assert isinstance(err, Exception)

    def test_str_contains_code(self) -> None:
        err = StaleInstrumentError(code="TXFD6", delivery_date=date(2026, 4, 15))
        assert "TXFD6" in str(err)


# ---------------------------------------------------------------------------
# Bootstrap-level integration test
# ---------------------------------------------------------------------------


class TestBootstrapRefusesStartOnStaleSubscription:
    """Bootstrap must refuse to start if any subscribed contract is stale."""

    def test_bootstrap_refuses_start_on_stale_subscription(self) -> None:
        """Importing and calling the bootstrap gate with a stale contract raises."""
        today = date(2026, 5, 5)
        # Simulate a stale TXO contract from April 2026
        stale_contract = _make_contract("TXO15000C6", date(2026, 4, 15))

        # The gate must block this subscription
        with pytest.raises(StaleInstrumentError) as exc_info:
            assert_not_expired(stale_contract, today=today)

        assert exc_info.value.code == "TXO15000C6"

    def test_bootstrap_allows_rollover_day_contract(self) -> None:
        """Bootstrap must accept a same-day expiry contract (rollover day)."""
        today = date(2026, 5, 5)
        rollover_contract = _make_contract("TMFE6", today)

        # Must succeed without exception; function returns None.
        assert assert_not_expired(rollover_contract, today=today) is None


# ---------------------------------------------------------------------------
# Iteration helper used by services/bootstrap.py post-connect hook
# ---------------------------------------------------------------------------


class TestAssertNoStaleSubscriptions:
    """Tests for the broker-agnostic iteration helper."""

    def test_passes_when_all_contracts_active(self) -> None:
        today = date(2026, 5, 5)
        symbols = [
            {"code": "TMFR1", "exchange": "TAIFEX", "product_type": "future"},
            {"code": "TXFR1", "exchange": "TAIFEX", "product_type": "future"},
        ]

        def _lookup(exch: str, code: str, ptype: str | None) -> SimpleNamespace:
            return _make_contract(code, today + datetime.timedelta(days=10))

        log = MagicMock()
        assert_no_stale_subscriptions(symbols, _lookup, today=today, log=log)
        assert not log.error.called

    def test_raises_and_logs_on_first_stale_contract(self) -> None:
        today = date(2026, 5, 5)
        symbols = [
            {"code": "TMFR1", "exchange": "TAIFEX", "product_type": "future"},
            {"code": "TXO15000C6", "exchange": "TAIFEX", "product_type": "option"},
        ]

        def _lookup(exch: str, code: str, ptype: str | None) -> SimpleNamespace:
            if code == "TXO15000C6":
                return _make_contract(code, date(2026, 4, 15))  # 20 days expired
            return _make_contract(code, today + datetime.timedelta(days=30))

        log = MagicMock()
        with pytest.raises(StaleInstrumentError) as exc_info:
            assert_no_stale_subscriptions(symbols, _lookup, today=today, log=log)

        assert exc_info.value.code == "TXO15000C6"
        log.error.assert_called_once()
        event_name, _, kwargs = log.error.mock_calls[0]
        assert log.error.mock_calls[0].args[0] == "stale_instrument_subscription_blocked"
        assert log.error.mock_calls[0].kwargs["code"] == "TXO15000C6"
        assert log.error.mock_calls[0].kwargs["delivery_date"] == "2026-04-15"

    def test_skips_symbols_without_resolved_contract(self) -> None:
        today = date(2026, 5, 5)
        symbols = [{"code": "GHOST", "exchange": "TAIFEX", "product_type": "future"}]
        log = MagicMock()

        # Contract resolution returns None → helper must NOT raise (let preflight handle it)
        assert_no_stale_subscriptions(symbols, lambda *_: None, today=today, log=log)
        assert not log.error.called

    def test_skips_malformed_symbol_entries(self) -> None:
        today = date(2026, 5, 5)
        symbols = [
            {"exchange": "TAIFEX"},  # no code
            {"code": "TMFR1"},  # no exchange
            "not-a-dict",  # not a mapping
            {"code": "OK", "exchange": "TAIFEX"},  # valid → resolved
        ]

        def _lookup(exch: str, code: str, ptype: str | None) -> SimpleNamespace:
            return _make_contract(code, today + datetime.timedelta(days=10))

        log = MagicMock()
        assert_no_stale_subscriptions(symbols, _lookup, today=today, log=log)
        assert not log.error.called

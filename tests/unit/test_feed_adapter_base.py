"""Tests for feed_adapter._base shared broker abstractions."""

from __future__ import annotations

import time
from typing import Any

import pytest

# ------------------------------------------------------------------ #
# BaseBrokerSessionRuntime
# ------------------------------------------------------------------ #


class TestBaseBrokerSessionRuntime:
    """Tests for the abstract session runtime base class."""

    def test_cannot_instantiate_directly(self) -> None:
        """BaseBrokerSessionRuntime is abstract and must not be instantiated."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        with pytest.raises(TypeError):
            BaseBrokerSessionRuntime()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self) -> None:
        """A concrete subclass implementing all abstract methods can be created."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class ConcreteSession(BaseBrokerSessionRuntime):
            __slots__ = ("_login_called",)

            def __init__(self) -> None:
                super().__init__(reconnect_cooldown_s=1.0, login_retry_max=2)
                self._login_called = 0

            def _do_login(self, **credentials: Any) -> bool:
                self._login_called += 1
                return credentials.get("key") == "valid"

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                self._do_logout()
                return self.login_with_retry()

            def _resolve_credentials(self) -> dict[str, Any]:
                return {"key": "valid"}

        s = ConcreteSession()
        assert not s.is_logged_in
        assert s.last_login_error is None

    def test_login_with_retry_succeeds(self) -> None:
        """login_with_retry should succeed on first try when _do_login returns True."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class OkSession(BaseBrokerSessionRuntime):
            def _do_login(self, **cred: Any) -> bool:
                return True

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                return True

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = OkSession(login_retry_max=3)
        assert s.login_with_retry()
        assert s.is_logged_in

    def test_login_with_retry_exhausts(self) -> None:
        """login_with_retry should return False when all attempts fail."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class FailSession(BaseBrokerSessionRuntime):
            def _do_login(self, **cred: Any) -> bool:
                self._last_login_error = "denied"
                return False

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                return False

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = FailSession(login_retry_max=2, backoff_base_s=0.01)
        assert not s.login_with_retry()
        assert not s.is_logged_in
        assert s.last_login_error == "denied"

    def test_reconnect_cooldown_blocks(self) -> None:
        """reconnect should be blocked during cooldown period."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class StubSession(BaseBrokerSessionRuntime):
            def __init__(self) -> None:
                super().__init__(reconnect_cooldown_s=10.0)
                self.reconnect_count = 0

            def _do_login(self, **cred: Any) -> bool:
                return True

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                self.reconnect_count += 1
                return True

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = StubSession()
        # First reconnect should succeed
        assert s.reconnect(reason="test")
        assert s.reconnect_count == 1

        # Second reconnect within cooldown should be blocked
        assert not s.reconnect(reason="test2")
        assert s.reconnect_count == 1

    def test_reconnect_force_bypasses_cooldown(self) -> None:
        """reconnect with force=True should bypass the cooldown."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class StubSession(BaseBrokerSessionRuntime):
            def __init__(self) -> None:
                super().__init__(reconnect_cooldown_s=10.0)
                self.reconnect_count = 0

            def _do_login(self, **cred: Any) -> bool:
                return True

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                self.reconnect_count += 1
                return True

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = StubSession()
        assert s.reconnect(reason="first")
        assert s.reconnect(reason="forced", force=True)
        assert s.reconnect_count == 2

    def test_logout_sets_logged_out(self) -> None:
        """logout should set is_logged_in to False."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class StubSession(BaseBrokerSessionRuntime):
            def _do_login(self, **cred: Any) -> bool:
                return True

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                return True

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = StubSession()
        s.login_with_retry()
        assert s.is_logged_in
        s.logout()
        assert not s.is_logged_in

    def test_snapshot(self) -> None:
        """snapshot should return a dict with session state."""
        from hft_platform.feed_adapter._base.session_runtime import BaseBrokerSessionRuntime

        class StubSession(BaseBrokerSessionRuntime):
            def _do_login(self, **cred: Any) -> bool:
                return True

            def _do_logout(self) -> None:
                pass

            def _do_reconnect(self, reason: str, force: bool) -> bool:
                return True

            def _resolve_credentials(self) -> dict[str, Any]:
                return {}

        s = StubSession(reconnect_cooldown_s=5.0)
        snap = s.snapshot()
        assert snap["logged_in"] is False
        assert snap["reconnect_cooldown_s"] == 5.0


# ------------------------------------------------------------------ #
# CooldownManager
# ------------------------------------------------------------------ #


class TestCooldownManager:
    """Tests for the reusable cooldown manager."""

    def test_first_acquire_succeeds(self) -> None:
        """First try_acquire should always succeed."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=10.0, name="test")
        assert cd.try_acquire()

    def test_second_acquire_within_cooldown_fails(self) -> None:
        """Second try_acquire within cooldown should fail."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=10.0, name="test")
        assert cd.try_acquire()
        assert not cd.try_acquire()

    def test_acquire_after_cooldown_succeeds(self) -> None:
        """try_acquire should succeed after cooldown has elapsed."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=0.05, name="test")
        assert cd.try_acquire()
        time.sleep(0.06)
        assert cd.try_acquire()

    def test_reset_allows_immediate_acquire(self) -> None:
        """reset should allow the next acquire to succeed immediately."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=100.0, name="test")
        assert cd.try_acquire()
        assert not cd.try_acquire()
        cd.reset()
        assert cd.try_acquire()

    def test_is_ready_property(self) -> None:
        """is_ready should reflect whether the cooldown has elapsed."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=0.05, name="test")
        assert cd.is_ready  # never acquired
        cd.try_acquire()
        assert not cd.is_ready
        time.sleep(0.06)
        assert cd.is_ready

    def test_cooldown_s_property(self) -> None:
        """cooldown_s should return the configured duration."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=2.5)
        assert cd.cooldown_s == 2.5

    def test_elapsed_s_before_any_acquire(self) -> None:
        """elapsed_s should be 0 before any acquire."""
        from hft_platform.feed_adapter._base.subscription_manager import CooldownManager

        cd = CooldownManager(cooldown_s=1.0)
        assert cd.elapsed_s == 0.0


# ------------------------------------------------------------------ #
# BaseQuoteWatchdog
# ------------------------------------------------------------------ #


class TestBaseQuoteWatchdog:
    """Tests for the shared quote watchdog base class."""

    def test_starts_and_stops(self) -> None:
        """Watchdog should start a thread and stop cleanly."""
        from hft_platform.feed_adapter._base.quote_runtime import BaseQuoteWatchdog

        wd = BaseQuoteWatchdog(timeout_s=0.1, check_interval_s=0.05)
        assert not wd.is_running
        wd.start()
        assert wd.is_running
        wd.stop()
        assert not wd.is_running

    def test_start_is_idempotent(self) -> None:
        """Calling start twice should not create a second thread."""
        from hft_platform.feed_adapter._base.quote_runtime import BaseQuoteWatchdog

        wd = BaseQuoteWatchdog(timeout_s=0.5, check_interval_s=0.1)
        wd.start()
        thread1 = wd._thread
        wd.start()
        thread2 = wd._thread
        assert thread1 is thread2
        wd.stop()

    def test_notify_data_updates_timestamp(self) -> None:
        """notify_data should update last_data_ts."""
        from hft_platform.feed_adapter._base.quote_runtime import BaseQuoteWatchdog

        wd = BaseQuoteWatchdog(timeout_s=1.0)
        assert wd.last_data_ts == 0.0
        wd.notify_data()
        assert wd.last_data_ts > 0.0

    def test_stall_callback_invoked(self) -> None:
        """on_stall should be called when no data arrives within timeout."""
        from hft_platform.feed_adapter._base.quote_runtime import BaseQuoteWatchdog

        stall_detected = []

        def on_stall(gap_s: float) -> None:
            stall_detected.append(gap_s)

        wd = BaseQuoteWatchdog(
            timeout_s=0.05,
            on_stall=on_stall,
            check_interval_s=0.03,
        )
        # Simulate data then stop feeding
        wd.notify_data()
        wd.start()
        time.sleep(0.15)
        wd.stop()

        assert len(stall_detected) > 0
        assert stall_detected[0] >= 0.05

    def test_no_stall_when_data_flows(self) -> None:
        """on_stall should NOT be called when data keeps arriving."""
        from hft_platform.feed_adapter._base.quote_runtime import BaseQuoteWatchdog

        stall_detected = []

        def on_stall(gap_s: float) -> None:
            stall_detected.append(gap_s)

        wd = BaseQuoteWatchdog(
            timeout_s=0.2,
            on_stall=on_stall,
            check_interval_s=0.05,
        )
        wd.notify_data()
        wd.start()
        # Keep feeding data
        for _ in range(5):
            time.sleep(0.04)
            wd.notify_data()
        wd.stop()

        assert len(stall_detected) == 0


# ------------------------------------------------------------------ #
# QuoteRuntimeProtocol
# ------------------------------------------------------------------ #


class TestQuoteRuntimeProtocol:
    """Tests for QuoteRuntimeProtocol runtime checking."""

    def test_is_runtime_checkable(self) -> None:
        """QuoteRuntimeProtocol should be usable with isinstance checks."""
        from hft_platform.feed_adapter._base.quote_runtime import QuoteRuntimeProtocol

        class FakeRuntime:
            def register_quote_callbacks(self, on_tick: Any, on_bidask: Any) -> None:
                pass

            def subscribe(self, symbols: list[str]) -> None:
                pass

            def unsubscribe(self, symbols: list[str]) -> None:
                pass

            def start_quote_watchdog(self, timeout_s: float = 30.0) -> None:
                pass

            def stop(self) -> None:
                pass

        assert isinstance(FakeRuntime(), QuoteRuntimeProtocol)

    def test_non_conformant_fails_check(self) -> None:
        """An object missing required methods should not satisfy the protocol."""
        from hft_platform.feed_adapter._base.quote_runtime import QuoteRuntimeProtocol

        class Incomplete:
            def subscribe(self, symbols: list[str]) -> None:
                pass

        assert not isinstance(Incomplete(), QuoteRuntimeProtocol)


# ------------------------------------------------------------------ #
# SubscriptionManagerProtocol
# ------------------------------------------------------------------ #


class TestSubscriptionManagerProtocol:
    """Tests for SubscriptionManagerProtocol runtime checking."""

    def test_is_runtime_checkable(self) -> None:
        """SubscriptionManagerProtocol should be usable with isinstance checks."""
        from hft_platform.feed_adapter._base.subscription_manager import (
            SubscriptionManagerProtocol,
        )

        class FakeMgr:
            def subscribe_basket(self, cb: Any) -> None:
                pass

            def resubscribe(self) -> bool:
                return True

            def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
                pass

        assert isinstance(FakeMgr(), SubscriptionManagerProtocol)


# ------------------------------------------------------------------ #
# Protocol re-exports from protocol.py
# ------------------------------------------------------------------ #


class TestProtocolReExports:
    """Verify that protocol.py re-exports the new protocols."""

    def test_quote_runtime_protocol_importable(self) -> None:
        from hft_platform.feed_adapter.protocol import QuoteRuntimeProtocol as QRP

        assert QRP is not None

    def test_subscription_manager_protocol_importable(self) -> None:
        from hft_platform.feed_adapter.protocol import SubscriptionManagerProtocol as SMP

        assert SMP is not None

    def test_existing_protocols_unchanged(self) -> None:
        from hft_platform.feed_adapter.protocol import BrokerClientProtocol, BrokerOrderCodec

        assert BrokerClientProtocol is not None
        assert BrokerOrderCodec is not None

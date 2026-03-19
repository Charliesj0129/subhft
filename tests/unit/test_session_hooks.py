"""Tests for WU-11: Pre/Post Market Session Hooks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.core.session_hooks import SessionHookManager, SessionPhase


class TestSessionHookManagerInit:
    """Test SessionHookManager initialization and configuration."""

    def test_disabled_by_default(self) -> None:
        mgr = SessionHookManager()
        assert mgr.enabled is False

    def test_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_SESSION_HOOKS_ENABLED", "1")
        mgr = SessionHookManager()
        assert mgr.enabled is True

    def test_custom_poll_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_SESSION_HOOKS_POLL_S", "10")
        mgr = SessionHookManager()
        assert mgr._poll_interval_s == 10.0

    def test_custom_hook_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_SESSION_HOOKS_TIMEOUT_S", "15")
        mgr = SessionHookManager()
        assert mgr._hook_timeout_s == 15.0


class TestSessionHookRegistration:
    """Test hook registration."""

    def test_register_pre_market(self) -> None:
        mgr = SessionHookManager()
        cb = MagicMock()
        mgr.register_pre_market("test_hook", cb)
        assert len(mgr._pre_market_hooks) == 1
        assert mgr._pre_market_hooks[0] == ("test_hook", cb)

    def test_register_post_market(self) -> None:
        mgr = SessionHookManager()
        cb = MagicMock()
        mgr.register_post_market("test_hook", cb)
        assert len(mgr._post_market_hooks) == 1
        assert mgr._post_market_hooks[0] == ("test_hook", cb)

    def test_register_multiple_hooks(self) -> None:
        mgr = SessionHookManager()
        mgr.register_pre_market("a", MagicMock())
        mgr.register_pre_market("b", MagicMock())
        mgr.register_post_market("c", MagicMock())
        assert len(mgr._pre_market_hooks) == 2
        assert len(mgr._post_market_hooks) == 1


class TestSessionPhaseDetection:
    """Test phase detection logic."""

    def test_detect_phase_market_open(self) -> None:
        mgr = SessionHookManager()
        mock_cal = MagicMock()
        mock_cal.is_trading_hours.return_value = True
        mgr._calendar = mock_cal

        phase = mgr._detect_phase()
        assert phase == SessionPhase.MARKET_OPEN

    def test_detect_phase_pre_market(self) -> None:
        mgr = SessionHookManager()
        mock_cal = MagicMock()
        mock_cal.is_trading_hours.return_value = False
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_close.return_value = None
        mgr._calendar = mock_cal

        phase = mgr._detect_phase()
        assert phase == SessionPhase.PRE_MARKET

    def test_detect_phase_post_market(self) -> None:
        import datetime as dt

        mgr = SessionHookManager()
        mock_cal = MagicMock()
        mock_cal.is_trading_hours.return_value = False
        mock_cal.is_trading_day.return_value = True

        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo("Asia/Taipei")
        except Exception:
            tz = dt.timezone(dt.timedelta(hours=8))

        # Set close time far in the past so now > close
        close_time = dt.datetime(2020, 1, 1, 13, 30, tzinfo=tz)
        mock_cal.get_session_close.return_value = close_time
        mgr._calendar = mock_cal

        phase = mgr._detect_phase()
        assert phase == SessionPhase.POST_MARKET


class TestSessionHookExecution:
    """Test hook firing logic."""

    @pytest.mark.asyncio
    async def test_fire_sync_hooks(self) -> None:
        mgr = SessionHookManager()
        cb = MagicMock()
        hooks = [("sync_hook", cb)]
        await mgr._fire_hooks(hooks, "test")
        cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_fire_async_hooks(self) -> None:
        mgr = SessionHookManager()
        cb = AsyncMock()
        hooks = [("async_hook", cb)]
        await mgr._fire_hooks(hooks, "test")
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hook_error_does_not_crash(self) -> None:
        mgr = SessionHookManager()
        cb = MagicMock(side_effect=ValueError("boom"))
        hooks = [("failing_hook", cb)]
        # Should not raise
        await mgr._fire_hooks(hooks, "test")

    @pytest.mark.asyncio
    async def test_hook_timeout_does_not_crash(self) -> None:
        mgr = SessionHookManager()
        mgr._hook_timeout_s = 0.01

        async def slow_hook():
            await asyncio.sleep(10)

        hooks = [("slow_hook", slow_hook)]
        # Should not raise
        await mgr._fire_hooks(hooks, "test")

    @pytest.mark.asyncio
    async def test_multiple_hooks_run_sequentially(self) -> None:
        mgr = SessionHookManager()
        order: list[str] = []

        def hook_a():
            order.append("a")

        def hook_b():
            order.append("b")

        hooks = [("a", hook_a), ("b", hook_b)]
        await mgr._fire_hooks(hooks, "test")
        assert order == ["a", "b"]


class TestSessionHookManagerRunLoop:
    """Test the main run loop."""

    @pytest.mark.asyncio
    async def test_run_disabled_returns_immediately(self) -> None:
        mgr = SessionHookManager()
        assert mgr.enabled is False
        # Should return without blocking
        await mgr.run()

    @pytest.mark.asyncio
    async def test_run_detects_transition_and_fires_hooks(self) -> None:
        """Verify that a PRE_MARKET -> MARKET_OPEN transition fires pre_market hooks."""
        mgr = SessionHookManager()
        mgr._enabled = True
        mgr._poll_interval_s = 0.01  # fast poll for test

        pre_hook = MagicMock()
        mgr.register_pre_market("test_pre", pre_hook)

        phases = iter(
            [
                SessionPhase.PRE_MARKET,  # initial detection
                SessionPhase.PRE_MARKET,  # first poll: no change
                SessionPhase.MARKET_OPEN,  # second poll: transition!
            ]
        )

        def mock_detect(self_arg):
            try:
                return next(phases)
            except StopIteration:
                mgr._running = False
                return SessionPhase.MARKET_OPEN

        with patch.object(type(mgr), "_detect_phase", mock_detect):
            await mgr.run()
        pre_hook.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_post_market_transition_fires_hooks(self) -> None:
        """Verify MARKET_OPEN -> POST_MARKET fires post_market hooks."""
        mgr = SessionHookManager()
        mgr._enabled = True
        mgr._poll_interval_s = 0.01

        post_hook = MagicMock()
        mgr.register_post_market("test_post", post_hook)

        phases = iter(
            [
                SessionPhase.MARKET_OPEN,  # initial
                SessionPhase.POST_MARKET,  # transition
            ]
        )

        def mock_detect(self_arg):
            try:
                return next(phases)
            except StopIteration:
                mgr._running = False
                return SessionPhase.POST_MARKET

        with patch.object(type(mgr), "_detect_phase", mock_detect):
            await mgr.run()
        post_hook.assert_called_once()

    def test_stop(self) -> None:
        mgr = SessionHookManager()
        mgr._running = True
        mgr.stop()
        assert mgr._running is False

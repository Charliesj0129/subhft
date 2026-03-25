"""Tests for autonomy-related notification templates and dispatcher methods."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hft_platform.notifications import templates
from hft_platform.notifications.dispatcher import NotificationDispatcher


class TestAutonomyTemplates:
    def test_render_autonomy_transition(self) -> None:
        msg = templates.render_autonomy_transition(
            scope="platform",
            from_mode="NORMAL",
            to_mode="HALT",
            reason="feed_gap",
        )
        assert "NORMAL" in msg
        assert "HALT" in msg
        assert "feed_gap" in msg

    def test_render_flatten_result_success(self) -> None:
        msg = templates.render_flatten_result(symbol="2330", qty=10, success=True)
        assert "2330" in msg
        assert "10" in msg

    def test_render_flatten_result_failure(self) -> None:
        msg = templates.render_flatten_result(symbol="2330", qty=5, success=False, error="timeout")
        assert "timeout" in msg

    def test_render_heartbeat(self) -> None:
        msg = templates.render_heartbeat(mode="NORMAL", uptime_s=3600.0, open_positions=3)
        assert "3600" in msg
        assert "3" in msg

    def test_render_session_phase(self) -> None:
        msg = templates.render_session_phase(phase="OPEN", detail="market open")
        assert "OPEN" in msg
        assert "market open" in msg

    def test_render_autonomy_daily_summary(self) -> None:
        msg = templates.render_autonomy_daily_summary(
            date_str="2026-03-25", transitions=5, halts=1, final_mode="NORMAL",
        )
        assert "2026-03-25" in msg
        assert "5" in msg


class TestDispatcherAutonomyMethods:
    def test_notify_autonomy_transition_sends(self) -> None:
        sender = MagicMock()
        sender.send = AsyncMock()
        dispatcher = NotificationDispatcher(sender)

        asyncio.get_event_loop().run_until_complete(
            dispatcher.notify_autonomy_transition(
                scope="platform", from_mode="NORMAL", to_mode="HALT", reason="test",
            )
        )
        sender.send.assert_awaited_once()
        call_args = sender.send.call_args
        assert call_args[1]["critical"] is True  # HALT is critical

    def test_notify_flatten_result_sends(self) -> None:
        sender = MagicMock()
        sender.send = AsyncMock()
        dispatcher = NotificationDispatcher(sender)

        asyncio.get_event_loop().run_until_complete(
            dispatcher.notify_flatten_result(symbol="2330", qty=10, success=True)
        )
        sender.send.assert_awaited_once()

    def test_notify_session_phase_sends(self) -> None:
        sender = MagicMock()
        sender.send = AsyncMock()
        dispatcher = NotificationDispatcher(sender)

        asyncio.get_event_loop().run_until_complete(
            dispatcher.notify_session_phase(phase="OPEN")
        )
        sender.send.assert_awaited_once()

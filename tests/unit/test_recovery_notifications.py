"""Tests for position recovery notification templates and dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_render_position_recovery():
    from hft_platform.notifications.templates import render_position_recovery

    msg = render_position_recovery(
        source="dual",
        loaded=5,
        corrected=1,
        mismatches=[{"symbol": "2330", "action": "corrected"}],
    )
    assert "dual" in msg
    assert "5" in msg
    assert "2330" in msg


def test_render_position_recovery_failed():
    from hft_platform.notifications.templates import render_position_recovery_failed

    msg = render_position_recovery_failed(
        source="dual",
        reason="Side mismatch on 2330",
        mismatches=[{"symbol": "2330", "checkpoint_qty": 100, "broker_qty": -50}],
    )
    assert "HALT" in msg or "失敗" in msg
    assert "2330" in msg


def test_notify_position_recovery_sends_non_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    d = NotificationDispatcher(sender=sender)
    asyncio.run(d.notify_position_recovery(source="dual", loaded=3, corrected=0, mismatches=[]))
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is False


def test_notify_position_recovery_failed_sends_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    d = NotificationDispatcher(sender=sender)
    asyncio.run(d.notify_position_recovery_failed(source="dual", reason="test", mismatches=[]))
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is True

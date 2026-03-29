"""Unit tests for bot command handlers and access control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

OWNER_CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)
    # Reset cached owner ID so monkeypatch takes effect
    import hft_platform.bot.app as bot_app
    bot_app._OWNER_CHAT_ID = 0


def _make_update(chat_id: int, text: str = "/start") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.args = []
    return ctx


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_owner_allowed(self) -> None:
        from hft_platform.bot.app import owner_only

        @owner_only
        async def dummy_handler(update, context):
            await update.message.reply_text("ok")

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await dummy_handler(update, ctx)
        update.message.reply_text.assert_any_call("ok")

    @pytest.mark.asyncio
    async def test_non_owner_rejected(self) -> None:
        from hft_platform.bot.app import owner_only

        called = False

        @owner_only
        async def dummy_handler(update, context):
            nonlocal called
            called = True

        update = _make_update(chat_id=99999)
        ctx = _make_context()
        await dummy_handler(update, ctx)
        assert not called
        update.message.reply_text.assert_called_once_with("未授權")

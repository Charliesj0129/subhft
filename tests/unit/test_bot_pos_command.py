"""Tests for /pos Telegram bot command."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

OWNER_CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)
    # Reset cached owner ID so monkeypatch takes effect
    import hft_platform.bot.app as bot_app

    bot_app._OWNER_CHAT_ID = 0


def _make_update(chat_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


@dataclass(slots=True)
class FakePosition:
    account_id: str
    strategy_id: str
    symbol: str
    net_qty: int
    avg_price_scaled: int = 0
    realized_pnl_scaled: int = 0
    fees_scaled: int = 0
    last_update_ts: int = 0


def _make_fake_store(positions: dict):
    store = MagicMock()
    store.positions = positions
    store._recovery_positions = {}
    return store


@pytest.mark.asyncio
async def test_pos_command_shows_all_strategies():
    """'/pos' with no args shows all strategies grouped."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2, 200000000),
        "acc:r47_maker:TMFD6": FakePosition("acc", "r47_maker", "TMFD6", 1, 190000000),
        "acc:MANUAL:TXFD6": FakePosition("acc", "MANUAL", "TXFD6", 1, 205000000),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "r47_maker" in reply
        assert "MANUAL" in reply
        assert "TXFD6" in reply
        assert "TMFD6" in reply


@pytest.mark.asyncio
async def test_pos_command_filters_by_strategy():
    """'/pos r47_maker' shows only that strategy's positions."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2),
        "acc:MANUAL:TXFD6": FakePosition("acc", "MANUAL", "TXFD6", 1),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update()
        context = MagicMock()
        context.args = ["r47_maker"]

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "r47_maker" in reply
        assert "MANUAL" not in reply


@pytest.mark.asyncio
async def test_pos_command_empty_positions():
    """'/pos' with no open positions shows empty message."""
    from hft_platform.bot.handlers import cmd_pos

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store({})):
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply or "empty" in reply.lower() or "0" in reply


@pytest.mark.asyncio
async def test_pos_command_store_not_connected():
    """'/pos' when position store is None shows not-connected message."""
    from hft_platform.bot.handlers import cmd_pos

    with patch("hft_platform.bot.handlers._get_position_store", return_value=None):
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "Position store" in reply or "未連接" in reply


@pytest.mark.asyncio
async def test_pos_command_skips_zero_qty_positions():
    """'/pos' skips positions with net_qty == 0 (flat positions)."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2),
        "acc:r47_maker:TMFD6": FakePosition("acc", "r47_maker", "TMFD6", 0),  # flat
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "TXFD6" in reply
        assert "TMFD6" not in reply


@pytest.mark.asyncio
async def test_pos_command_filter_no_match_shows_empty():
    """'/pos unknown_strategy' when that strategy has no positions shows empty message."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 1),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update()
        context = MagicMock()
        context.args = ["nonexistent_strategy"]

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply


@pytest.mark.asyncio
async def test_pos_command_aggregate_footer_shown_for_multiple_strategies():
    """'/pos' shows aggregate footer when multiple strategies hold the same symbol."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2),
        "acc:MANUAL:TXFD6": FakePosition("acc", "MANUAL", "TXFD6", 1),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        # Aggregate total footer should appear
        assert "合計" in reply


@pytest.mark.asyncio
async def test_pos_command_unauthorized_user_rejected():
    """Non-owner chat ID should receive '未授權' and handler body not executed."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {"acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 1)}

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = _make_update(chat_id=99999)  # not the owner
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert reply == "未授權"

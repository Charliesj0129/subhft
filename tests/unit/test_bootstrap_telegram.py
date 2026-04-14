"""Tests for TelegramSender enabled flag wiring in bootstrap.

Verifies that:
- TelegramSender(enabled=True) with env vars set produces _enabled=True
- TelegramSender() (default enabled=False) produces _enabled=False even with env vars set
"""

from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from hft_platform.notifications import telegram as _tg_mod
from hft_platform.notifications.telegram import TelegramSender


class TestTelegramSenderEnabled:
    def test_enabled_true_with_env_vars_activates_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token-123")
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "99999")
        # Ensure aiohttp presence check passes (may be None in test env)
        monkeypatch.setattr(_tg_mod, "aiohttp", MagicMock())

        sender = TelegramSender(enabled=True)

        assert sender._enabled is True

    def test_enabled_false_default_disables_sender_even_with_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token-123")
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "99999")

        sender = TelegramSender()

        assert sender._enabled is False

    def test_enabled_true_without_token_stays_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)

        sender = TelegramSender(enabled=True)

        assert sender._enabled is False

    def test_enabled_true_without_chat_id_stays_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token-123")
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)

        sender = TelegramSender(enabled=True)

        assert sender._enabled is False

    def test_explicit_credentials_with_enabled_true_activates_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_tg_mod, "aiohttp", MagicMock())
        sender = TelegramSender(
            bot_token="explicit-token",
            chat_id="12345",
            enabled=True,
        )

        assert sender._enabled is True

    def test_explicit_credentials_with_enabled_false_stays_disabled(self) -> None:
        sender = TelegramSender(
            bot_token="explicit-token",
            chat_id="12345",
            enabled=False,
        )

        assert sender._enabled is False

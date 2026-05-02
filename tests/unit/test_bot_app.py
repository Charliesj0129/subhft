"""Tests for bot app startup wiring."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch


def test_latest_report_context_dataclass_roundtrip() -> None:
    import hft_platform.bot.app as bot_app

    ctx = bot_app.LatestReportContext(
        symbol="TXFD6",
        session="day",
        date="2026-04-07",
        dossier=object(),
        decision=object(),
    )

    assert ctx.symbol == "TXFD6"
    assert ctx.session == "day"


def test_latest_manual_report_context_defaults_to_none() -> None:
    import hft_platform.bot.app as bot_app

    assert bot_app.latest_manual_report_context is None


def test_create_app_registers_report_rule_and_ask(monkeypatch) -> None:
    import hft_platform.bot.app as mod

    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    fake_app = MagicMock()
    fake_builder = MagicMock()
    fake_builder.token.return_value.build.return_value = fake_app
    fake_ext = ModuleType("telegram.ext")
    fake_ext.Application = MagicMock()
    fake_ext.Application.builder.return_value = fake_builder
    fake_ext.CommandHandler = lambda name, fn: (name, fn)
    fake_telegram = ModuleType("telegram")
    fake_telegram.ext = fake_ext

    with (
        patch.dict(sys.modules, {"telegram": fake_telegram, "telegram.ext": fake_ext}),
        patch("hft_platform.bot.scheduler.schedule_jobs") as mock_schedule,
    ):
        mod.create_app()

    commands = [call.args[0][0] for call in fake_app.add_handler.call_args_list]
    assert "report_rule" in commands
    assert "ask" in commands
    mock_schedule.assert_called_once()


def test_main_starts_health_server_before_polling() -> None:
    """Bot main() should start the background health server."""
    import hft_platform.bot.app as mod

    fake_app = MagicMock()

    with (
        patch.object(mod, "_start_health_server_background") as mock_health,
        patch.object(mod, "create_app", return_value=fake_app),
    ):
        mod.main()

    mock_health.assert_called_once_with()
    fake_app.run_polling.assert_called_once_with(drop_pending_updates=True)

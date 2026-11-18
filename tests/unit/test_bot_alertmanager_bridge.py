"""The Alertmanager -> Telegram bridge runs in the hft-bot process, not the engine.

On 2026-11-11 the bridge lived inside hft-engine; when the engine went down
Alertmanager could not deliver HFTEngineDown (18 notifications given up with
"lookup hft-engine"). hft-bot is always on, so the bridge starts on its loop.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hft_platform.bot.app as mod

_BRIDGE_CLASS = "hft_platform.notifications.alertmanager_bridge.AlertmanagerBridge"


class _FakeBridge:
    """Stands in for AlertmanagerBridge: records construction, blocks in run() like serve_forever."""

    instances: list[_FakeBridge] = []

    def __init__(self) -> None:
        self.run_calls = 0
        self.close = AsyncMock()
        _FakeBridge.instances.append(self)

    async def run(self) -> None:
        self.run_calls += 1
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def _isolated_bridge_state(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # Tests inherit the operator's environment through make: pin the kill-switch,
    # and restore the module-level bridge references afterwards.
    monkeypatch.delenv(mod._ALERTMANAGER_BRIDGE_ENV, raising=False)
    monkeypatch.setattr(mod, "_alertmanager_bridge", None)
    monkeypatch.setattr(mod, "_alertmanager_bridge_task", None)
    _FakeBridge.instances = []
    log = MagicMock()
    monkeypatch.setattr(mod, "_log", log)
    return log


@pytest.mark.asyncio
async def test_post_init_starts_the_bridge_once_on_the_running_loop(_isolated_bridge_state: MagicMock) -> None:
    with patch(_BRIDGE_CLASS, _FakeBridge):
        try:
            await mod._post_init(MagicMock())
            await asyncio.sleep(0)  # let the scheduled task reach run()

            assert len(_FakeBridge.instances) == 1
            assert _FakeBridge.instances[0].run_calls == 1
            _isolated_bridge_state.info.assert_any_call("alertmanager_bridge_started_in_bot")
        finally:
            await mod._post_shutdown(MagicMock())


@pytest.mark.asyncio
async def test_post_init_keeps_a_reference_to_the_running_bridge_task() -> None:
    with patch(_BRIDGE_CLASS, _FakeBridge):
        try:
            await mod._post_init(MagicMock())
            await asyncio.sleep(0)

            task = mod._alertmanager_bridge_task
            assert task is not None
            assert not task.done()
            assert mod._alertmanager_bridge is _FakeBridge.instances[0]
        finally:
            await mod._post_shutdown(MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "OFF"])
async def test_post_init_does_not_start_the_bridge_when_the_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(mod._ALERTMANAGER_BRIDGE_ENV, value)
    with patch(_BRIDGE_CLASS, _FakeBridge):
        await mod._post_init(MagicMock())
        await asyncio.sleep(0)

    assert _FakeBridge.instances == []
    assert mod._alertmanager_bridge_task is None


@pytest.mark.asyncio
async def test_post_init_starts_the_bridge_when_the_kill_switch_is_explicitly_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(mod._ALERTMANAGER_BRIDGE_ENV, "1")
    with patch(_BRIDGE_CLASS, _FakeBridge):
        try:
            await mod._post_init(MagicMock())
            await asyncio.sleep(0)

            assert len(_FakeBridge.instances) == 1
        finally:
            await mod._post_shutdown(MagicMock())


@pytest.mark.asyncio
async def test_post_init_logs_a_warning_and_survives_a_bridge_constructor_failure(
    _isolated_bridge_state: MagicMock,
) -> None:
    with patch(_BRIDGE_CLASS, side_effect=RuntimeError("TelegramSender boom")):
        await mod._post_init(MagicMock())  # must not raise

    warned = [call.args[0] for call in _isolated_bridge_state.warning.call_args_list]
    assert "alertmanager_bridge_start_failed_in_bot" in warned
    assert mod._alertmanager_bridge_task is None
    started = [call.args[0] for call in _isolated_bridge_state.info.call_args_list]
    assert "alertmanager_bridge_started_in_bot" not in started


@pytest.mark.asyncio
async def test_post_init_warns_when_the_bridge_stops_on_its_own(_isolated_bridge_state: MagicMock) -> None:
    class _ReturnsAtOnce(_FakeBridge):
        async def run(self) -> None:  # e.g. bind failure: run() logs and returns
            self.run_calls += 1

    with patch(_BRIDGE_CLASS, _ReturnsAtOnce):
        await mod._post_init(MagicMock())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    warned = [call.args[0] for call in _isolated_bridge_state.warning.call_args_list]
    assert "alertmanager_bridge_task_ended_in_bot" in warned
    await mod._post_shutdown(MagicMock())


@pytest.mark.asyncio
async def test_post_shutdown_cancels_the_bridge_task_and_closes_the_bridge() -> None:
    with patch(_BRIDGE_CLASS, _FakeBridge):
        await mod._post_init(MagicMock())
        await asyncio.sleep(0)
        task = mod._alertmanager_bridge_task
        assert task is not None

        await mod._post_shutdown(MagicMock())
        await asyncio.sleep(0)

    assert task.cancelled()
    _FakeBridge.instances[0].close.assert_awaited_once()
    assert mod._alertmanager_bridge_task is None


@pytest.mark.asyncio
async def test_post_shutdown_survives_a_bridge_close_failure(_isolated_bridge_state: MagicMock) -> None:
    with patch(_BRIDGE_CLASS, _FakeBridge):
        await mod._post_init(MagicMock())
        await asyncio.sleep(0)
        _FakeBridge.instances[0].close.side_effect = RuntimeError("session already closed")

        await mod._post_shutdown(MagicMock())  # must not raise

    warned = [call.args[0] for call in _isolated_bridge_state.warning.call_args_list]
    assert "alertmanager_bridge_close_failed_in_bot" in warned


@pytest.mark.asyncio
async def test_post_shutdown_without_a_started_bridge_is_a_no_op() -> None:
    await mod._post_shutdown(MagicMock())

    assert mod._alertmanager_bridge_task is None


def test_create_app_registers_the_bridge_hooks_with_the_application_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    fake_app: Any = MagicMock()
    fake_builder = MagicMock()
    fake_builder.token.return_value = fake_builder
    fake_builder.post_init.return_value = fake_builder
    fake_builder.post_shutdown.return_value = fake_builder
    fake_builder.build.return_value = fake_app
    fake_ext = ModuleType("telegram.ext")
    fake_ext.Application = MagicMock()
    fake_ext.Application.builder.return_value = fake_builder
    fake_ext.CommandHandler = lambda name, fn: (name, fn)
    fake_telegram = ModuleType("telegram")
    fake_telegram.ext = fake_ext

    with (
        patch.dict(sys.modules, {"telegram": fake_telegram, "telegram.ext": fake_ext}),
        patch("hft_platform.bot.scheduler.schedule_jobs"),
    ):
        built = mod.create_app()

    assert built is fake_app
    fake_builder.post_init.assert_called_once_with(mod._post_init)
    fake_builder.post_shutdown.assert_called_once_with(mod._post_shutdown)


def test_bootstrap_no_longer_constructs_the_alertmanager_bridge() -> None:
    """The engine's bridge died with the engine; it now lives in hft-bot."""
    source = Path("src/hft_platform/services/bootstrap.py").read_text(encoding="utf-8")
    assert "AlertmanagerBridge" not in source
    assert "alertmanager_bridge" not in source
    assert "hft-bot:8081" in source


def test_bot_app_constructs_the_alertmanager_bridge() -> None:
    source = Path("src/hft_platform/bot/app.py").read_text(encoding="utf-8")
    assert "import AlertmanagerBridge" in source
    assert "bridge = AlertmanagerBridge()" in source

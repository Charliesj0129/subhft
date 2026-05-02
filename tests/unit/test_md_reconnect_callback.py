"""Tests for MarketDataReconnectMixin on_reconnect callbacks."""

from unittest.mock import MagicMock

import pytest

from hft_platform.services._md_reconnect import MarketDataReconnectMixin


class _FakeMDService(MarketDataReconnectMixin):
    """Minimal stub implementing attributes used by _trigger_reconnect."""

    def __init__(self) -> None:
        self._last_reconnect_ts = 0.0
        self.reconnect_cooldown_s = 0.0
        self.reconnect_timeout_s = 30.0
        self.last_event_ts = 0.0
        self.last_event_mono = 0.0
        self._resubscribe_attempts = 0
        self.state = "INIT"
        self.client = MagicMock()
        self.client.reconnect = MagicMock(return_value=True)
        self.metrics_registry = None
        self.lob = None
        self.feature_engine = None
        self._on_reconnect_callbacks: list = []

    def _set_state(self, new_state: object) -> None:
        self.state = new_state

    def _within_reconnect_window(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_register_and_fire_sync_callback():
    svc = _FakeMDService()
    called_with: list[str] = []
    svc.register_on_reconnect(lambda reason: called_with.append(reason))

    ok = await svc._trigger_reconnect(gap=120.0, reason="test_gap")

    assert ok is True
    assert called_with == ["test_gap"]


@pytest.mark.asyncio
async def test_register_and_fire_async_callback():
    svc = _FakeMDService()
    called_with: list[str] = []

    async def async_cb(reason: str) -> None:
        called_with.append(reason)

    svc.register_on_reconnect(async_cb)

    ok = await svc._trigger_reconnect(gap=120.0, reason="async_test")

    assert ok is True
    assert called_with == ["async_test"]


@pytest.mark.asyncio
async def test_callback_error_does_not_block_reconnect():
    svc = _FakeMDService()
    second_called: list[str] = []

    def bad_cb(reason: str) -> None:
        raise RuntimeError("boom")

    svc.register_on_reconnect(bad_cb)
    svc.register_on_reconnect(lambda reason: second_called.append(reason))

    ok = await svc._trigger_reconnect(gap=120.0, reason="err_test")

    assert ok is True
    # Second callback still fires despite first raising
    assert second_called == ["err_test"]


@pytest.mark.asyncio
async def test_callbacks_not_fired_on_failed_reconnect():
    svc = _FakeMDService()
    svc.client.reconnect = MagicMock(return_value=False)
    called: list[str] = []
    svc.register_on_reconnect(lambda reason: called.append(reason))

    ok = await svc._trigger_reconnect(gap=120.0, reason="fail_test")

    assert ok is False
    assert called == []

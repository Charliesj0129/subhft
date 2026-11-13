"""A warm-up that cannot get a probe through must ask for a relogin.

THESHOW, 2026-11-13 (same on 11-09 and 11-10): the order session was not
established after a heartbeat-gap relogin at 00:30. Five warm-up windows of ten
probes each all failed (00:40:13-00:44:28), each logged
``order_session_warmup_exhausted`` and nothing else happened. At 00:45:00 R47's
first six ``place_order`` calls hit ``SessionNotEstablished``, went to the DLQ
and tripped the breaker. Only then did the order watchdog relogin, and the
session was warm 2.2 s later.

So a relogin heals it in seconds; the warm-up just never asked for one. These
tests pin that it now asks (through a hook, never the broker directly) once
enough whole windows have failed, and that asking is the guarded path
(``reconnect(force=False)`` plus the execution-callback restore).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.services.order_session_warmup import OrderSessionWarmup
from hft_platform.services.system import HFTSystem

OPEN_AHEAD = (False, True)  # closed now, open one lead away: warm
IN_SESSION = (True, True)  # re-arms the clock latch after a successful window


class ScriptedClient:
    """Probe answers come from a script; past its end the session stays down."""

    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.calls = 0

    def warm_order_session(self) -> bool:
        self.calls += 1
        return self.results.pop(0) if self.results else False


async def _drive(warmup: OrderSessionWarmup, script: list[tuple[bool, bool]]) -> None:
    """Run the loop through ``script`` one window per tick, then stop it.

    The source hands out one window per tick and sets ``done`` on the first tick
    after the script, which is only reached once the previous window (and any
    hook it triggered) has finished.
    """
    done = asyncio.Event()
    pending = list(script)

    def _source() -> tuple[bool, bool]:
        if pending:
            return pending.pop(0)
        done.set()
        return IN_SESSION

    warmup._window_source = _source
    task = asyncio.create_task(warmup.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=5.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _warmup(client: ScriptedClient, **kwargs: Any) -> OrderSessionWarmup:
    return OrderSessionWarmup(client, tick_s=0.0, attempts=1, retry_interval_s=0.0, **kwargs)


class TestExhaustedWindowsRequestARelogin:
    @pytest.mark.asyncio
    async def test_two_exhausted_windows_request_a_reconnect_once(self) -> None:
        client = ScriptedClient([])  # never answers
        hook = AsyncMock()
        warmup = _warmup(client, on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD, OPEN_AHEAD])

        assert client.calls == 2
        hook.assert_awaited_once_with("order_warmup_exhausted")

    @pytest.mark.asyncio
    async def test_one_exhausted_window_does_not_request_a_reconnect(self) -> None:
        """A single slow handshake is the warm-up's own job, not a relogin."""
        hook = AsyncMock()
        warmup = _warmup(ScriptedClient([]), on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD])

        hook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_persistently_dead_session_asks_once_per_n_windows(self) -> None:
        """Not once per window: the reconnect path's cooldown is the storm guard,
        and the request rate must not be the thing that tests it."""
        hook = AsyncMock()
        warmup = _warmup(ScriptedClient([]), on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD] * 5)

        assert hook.await_count == 2  # after window 2 and window 4

    @pytest.mark.asyncio
    async def test_a_successful_window_resets_the_exhausted_count(self) -> None:
        """fail, ok, fail is two non-consecutive failures: no relogin."""
        client = ScriptedClient([False, True, False])
        hook = AsyncMock()
        warmup = _warmup(client, on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD, OPEN_AHEAD, IN_SESSION, OPEN_AHEAD])

        assert client.calls == 3
        hook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_threshold_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_WARMUP_RECONNECT_AFTER", "1")
        hook = AsyncMock()
        warmup = _warmup(ScriptedClient([]), on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD])

        hook.assert_awaited_once_with("order_warmup_exhausted")

    @pytest.mark.asyncio
    async def test_a_hook_attached_after_construction_is_used(self) -> None:
        """HFTSystem attaches the hook once the order watchdog exists."""
        hook = AsyncMock()
        warmup = _warmup(ScriptedClient([]))
        warmup.on_exhausted = hook

        await _drive(warmup, [OPEN_AHEAD, OPEN_AHEAD])

        hook.assert_awaited_once_with("order_warmup_exhausted")


class TestAHookFailureNeverEndsTheClock:
    @pytest.mark.asyncio
    async def test_a_raising_hook_does_not_stop_the_loop(self) -> None:
        client = ScriptedClient([])
        hook = AsyncMock(side_effect=RuntimeError("login slot busy"))
        warmup = _warmup(client, on_exhausted=hook)

        await _drive(warmup, [OPEN_AHEAD] * 4)

        assert client.calls == 4, "the loop must keep probing after the hook raised"
        assert hook.await_count == 2

    @pytest.mark.asyncio
    async def test_no_hook_is_not_an_error(self) -> None:
        """Without an order adapter there is no watchdog and no hook."""
        client = ScriptedClient([])
        warmup = _warmup(client)

        await _drive(warmup, [OPEN_AHEAD] * 3)

        assert warmup.on_exhausted is None
        assert client.calls == 3


class _StubSystem:
    """Only what the shared reconnect needs; same shape as the watchdog stubs."""

    _reconnect_order_session = HFTSystem._reconnect_order_session

    def __init__(self, *, with_callbacks: bool = True, reconnect_result: bool = True) -> None:
        self.reconnect = MagicMock(return_value=reconnect_result)
        self.set_execution_callbacks = MagicMock()
        self.order_client = SimpleNamespace(
            reconnect=self.reconnect,
            set_execution_callbacks=self.set_execution_callbacks,
        )
        if with_callbacks:
            self._exec_callbacks = (lambda state, payload: None, lambda payload: None)
        else:
            self._exec_callbacks = None


class TestTheSharedReconnectIsTheGuardedPath:
    @pytest.mark.asyncio
    async def test_the_warmup_reason_reconnects_unforced_and_restores_fill_callbacks(self) -> None:
        system = _StubSystem()

        result = await system._reconnect_order_session("order_warmup_exhausted")

        assert result == "reconnected"
        system.reconnect.assert_called_once_with(reason="order_warmup_exhausted", force=False)
        system.set_execution_callbacks.assert_called_once_with(
            on_order=system._exec_callbacks[0], on_deal=system._exec_callbacks[1]
        )

    @pytest.mark.asyncio
    async def test_a_reconnect_without_callbacks_reports_and_registers_nothing(self) -> None:
        system = _StubSystem(with_callbacks=False)

        result = await system._reconnect_order_session("order_warmup_exhausted")

        assert result == "no_callbacks"
        system.set_execution_callbacks.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_gated_reconnect_leaves_the_callbacks_alone(self) -> None:
        """The orchestrator's cooldown said no; the session is untouched."""
        system = _StubSystem(reconnect_result=False)

        result = await system._reconnect_order_session("order_warmup_exhausted")

        assert result == "reconnect_failed"
        system.set_execution_callbacks.assert_not_called()

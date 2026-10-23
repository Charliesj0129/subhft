"""``kill_switch_active`` says whether the kill-switch hold is set -- the thing the halt path actually obeys.

The incident protocol tells the operator to verify a kill with a metric. Before this gauge there was none, so the
only evidence was a log line. The gauge is written inside ``StormGuard.set_kill_switch_hold`` and the supervisor
calls that every tick, so it cannot drift from the hold.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, PropertyMock

import pytest

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk import kill_switch
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.services import system as system_module
from hft_platform.services.system import HFTSystem


def _gauge() -> float:
    return MetricsRegistry.get().kill_switch_active._value.get()


def test_the_gauge_follows_the_hold():
    guard = StormGuard()

    guard.set_kill_switch_hold(True)
    assert _gauge() == 1

    guard.set_kill_switch_hold(False)
    assert _gauge() == 0


def test_setting_the_same_hold_again_keeps_the_gauge():
    """The supervisor re-asserts the hold every tick; the gauge must not flap or reset."""
    guard = StormGuard()

    guard.set_kill_switch_hold(True)
    guard.set_kill_switch_hold(True)

    assert _gauge() == 1


def test_a_gauge_failure_does_not_break_the_hold():
    guard = StormGuard()
    broken = MagicMock()
    broken.kill_switch_active.set.side_effect = RuntimeError("registry gone")
    guard.metrics = broken

    guard.set_kill_switch_hold(True)

    assert guard.kill_switch_hold is True


class _NoSleep:
    """``asyncio`` as the system module sees it, with the 1 Hz tick sleep removed."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_delay, *_args, **_kwargs):
        return None


def _tick_system(guard: StormGuard, ticks: int) -> MagicMock:
    """A stand-in ``self`` for ``HFTSystem._supervise``: every collaborator is a MagicMock, the guard is real."""
    system = MagicMock()
    system.storm_guard = guard
    system._recovery_halted = False
    system._loop_watchdog = None
    type(system).running = PropertyMock(side_effect=[True] * ticks + [False])
    return system


async def _run_supervise(system: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(system_module, "asyncio", _NoSleep())
    monkeypatch.setenv("HFT_HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    try:
        await HFTSystem._supervise(system)
    except Exception:  # noqa: BLE001 - the stubbed collaborators cut the tick off after the kill-switch block
        pass


@pytest.mark.asyncio
async def test_a_latch_file_raises_the_gauge_on_the_next_supervise_tick_and_removing_it_clears_it(
    tmp_path, monkeypatch
):
    """The drill: write the latch, one tick -> HALT held and gauge 1; remove it, one tick -> gauge 0."""
    latch = tmp_path / "kill_switch"
    monkeypatch.setenv(kill_switch.PATH_ENV, str(latch))
    guard = StormGuard()
    assert guard.kill_switch_hold is False

    latch.write_text(json.dumps({"reason": "drill", "actor": "test"}))
    await _run_supervise(_tick_system(guard, ticks=1), monkeypatch, tmp_path)

    assert guard.state == StormGuardState.HALT
    assert guard.kill_switch_hold is True
    assert _gauge() == 1

    latch.unlink()
    await _run_supervise(_tick_system(guard, ticks=1), monkeypatch, tmp_path)

    assert guard.kill_switch_hold is False
    assert _gauge() == 0


@pytest.mark.asyncio
async def test_a_startup_recovery_halt_keeps_the_gauge_up_when_the_file_goes_away(tmp_path, monkeypatch):
    """A recovery halt pins the hold for the process lifetime; the gauge must say so, not follow the file."""
    monkeypatch.setenv(kill_switch.PATH_ENV, str(tmp_path / "kill_switch"))
    guard = StormGuard()
    guard.set_kill_switch_hold(True)  # what _on_startup_recovery_halt does
    system = _tick_system(guard, ticks=1)
    system._recovery_halted = True

    await _run_supervise(system, monkeypatch, tmp_path)

    assert guard.kill_switch_hold is True
    assert _gauge() == 1


@pytest.mark.asyncio
async def test_a_recovery_halt_rewrites_a_gauge_whose_first_write_failed_once_the_file_is_gone(tmp_path, monkeypatch):
    """The hold survives a failed gauge write; the supervisor must keep rewriting the gauge while the pin stands."""
    monkeypatch.setenv(kill_switch.PATH_ENV, str(tmp_path / "kill_switch"))  # no latch file
    guard = StormGuard()
    healthy = guard.metrics
    broken = MagicMock()
    broken.kill_switch_active.set.side_effect = RuntimeError("registry gone")
    guard.metrics = broken
    guard.set_kill_switch_hold(True)  # _on_startup_recovery_halt: hold set, gauge write fails
    guard.metrics = healthy
    healthy.kill_switch_active.set(0)  # what the scrape shows: the write never landed
    system = _tick_system(guard, ticks=1)
    system._recovery_halted = True

    await _run_supervise(system, monkeypatch, tmp_path)

    assert guard.kill_switch_hold is True
    assert _gauge() == 1


def test_the_boot_path_sets_the_hold_through_the_same_setter():
    """Boot reads the latch before ``_supervise`` runs; it must reach the gauge through ``set_kill_switch_hold``."""
    import inspect

    source = inspect.getsource(HFTSystem)

    # boot latch, recovery halt, supervise tick (file present), supervise tick (recovery pin)
    assert source.count("set_kill_switch_hold(True)") >= 4

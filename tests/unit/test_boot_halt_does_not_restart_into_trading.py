"""A startup position-recovery halt must not become a clean restart into trading.

Production, 2026-09-04::

    boot 1 (14:06:36Z)  checkpoint -4 vs broker 0 -> recovery halts
                        logger.critical(...); return        <- system.py
          |
          v  process exits 0 at 14:07:53.78Z -- a clean exit, to Docker
    shutdown writes a final checkpoint from an EMPTY store   <- evidence gone
          |
          v  restart: always -> RestartCount 0 -> 1
    boot 2 (14:08:01Z)  "Position recovery complete, loaded=0"
                        trading starts, the -4 silently discarded    X

Three separate things had to be true, and all three are fixed here:

1. the halt returned instead of latching, so nothing survived the process;
2. every checkpoint write stayed armed on the halt path, and the store is
   empty there by construction, so the write destroyed its own evidence;
3. the file latch was only ever read from ``_supervise()``, which does not run
   until every trading service is already up.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hft_platform.risk import kill_switch
from hft_platform.services.system import HFTSystem

SYSTEM_SRC = Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "services" / "system.py"


@pytest.fixture
def latch_path(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "kill_switch"
    monkeypatch.setenv(kill_switch.PATH_ENV, str(path))
    return path


class _SpyStormGuard:
    def __init__(self) -> None:
        self.halts: list[str] = []

    def trigger_halt(self, reason: str) -> None:
        self.halts.append(reason)


class _SpyCheckpointWriter:
    def __init__(self) -> None:
        self.writes = 0

    def write_checkpoint(self) -> None:
        self.writes += 1


def _system(**attrs) -> HFTSystem:
    """An HFTSystem with only the attributes the checkpoint/halt paths touch.

    ``__init__`` builds the whole platform; these paths are pure guards over
    four fields, so bypassing it keeps the test about the guard.
    """
    system = object.__new__(HFTSystem)
    system._recovery_halted = False
    system._halt_checkpoint_written = False
    system.checkpoint_writer = None
    system.storm_guard = _SpyStormGuard()
    for key, value in attrs.items():
        setattr(system, key, value)
    return system


# ---------------------------------------------------------------------------
# 1. The latch itself
# ---------------------------------------------------------------------------


class TestKillSwitchLatch:
    def test_activate_creates_the_directory_and_records_the_reason(self, latch_path):
        assert not latch_path.parent.exists()

        written = kill_switch.activate("startup_position_recovery:dual", actor="startup_position_recovery")

        assert written == str(latch_path)
        record = json.loads(latch_path.read_text())
        assert record["reason"] == "startup_position_recovery:dual"
        assert record["actor"] == "startup_position_recovery"
        assert record["timestamp_ns"] > 0

    def test_is_active_tracks_the_file(self, latch_path):
        assert kill_switch.is_active() is False
        kill_switch.activate("halt", actor="test")
        assert kill_switch.is_active() is True
        assert kill_switch.deactivate() is True
        assert kill_switch.is_active() is False

    def test_deactivate_reports_false_when_there_was_no_latch(self, latch_path):
        assert kill_switch.deactivate() is False

    def test_activate_overwrites_an_older_reason(self, latch_path):
        kill_switch.activate("first", actor="cli")
        kill_switch.activate("second", actor="startup_position_recovery")

        assert kill_switch.read_reason() == "second"

    def test_read_reason_falls_back_to_unknown_when_the_key_is_absent(self, latch_path):
        latch_path.parent.mkdir(parents=True)
        latch_path.write_text(json.dumps({"actor": "cli"}))

        assert kill_switch.read_reason() == "unknown"

    def test_path_follows_the_env_var_and_defaults_without_it(self, monkeypatch):
        monkeypatch.delenv(kill_switch.PATH_ENV, raising=False)
        assert kill_switch.kill_switch_path() == kill_switch.DEFAULT_PATH
        monkeypatch.setenv(kill_switch.PATH_ENV, "/custom/latch")
        assert kill_switch.kill_switch_path() == "/custom/latch"


# ---------------------------------------------------------------------------
# 2. The halt latches instead of returning
# ---------------------------------------------------------------------------


class TestStartupRecoveryHalt:
    def test_halt_latches_the_kill_switch_and_halts_stormguard(self, latch_path):
        system = _system()

        system._on_startup_recovery_halt(
            reason="startup_position_recovery:dual",
            source="dual",
            mismatches=[{"symbol": "TMFI6", "checkpoint_qty": -4, "broker_qty": 0, "action": "halt"}],
        )

        assert system._recovery_halted is True
        assert kill_switch.is_active() is True
        assert kill_switch.read_reason() == "startup_position_recovery:dual"
        assert system.storm_guard.halts == ["STARTUP_POSITION_RECOVERY: dual"]

    def test_halt_still_halts_in_memory_when_the_latch_cannot_be_written(self, tmp_path, monkeypatch):
        # A file the latch cannot be created under: the cross-restart latch is
        # lost, but refusing to trade *this* boot must not depend on it.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv(kill_switch.PATH_ENV, str(blocker / "kill_switch"))
        system = _system()

        system._on_startup_recovery_halt(reason="r", source="exception", mismatches=[], error="boom")

        assert system._recovery_halted is True
        assert system.storm_guard.halts == ["STARTUP_POSITION_RECOVERY: exception"]

    def test_recovery_halt_branch_no_longer_returns_out_of_run(self):
        # The defect was a bare ``return``: it exited run() with status 0, which
        # a restart policy reads as success. Nothing may reintroduce it.
        source = SYSTEM_SRC.read_text()
        halt_call = source.index("self._on_startup_recovery_halt(")
        window = source[halt_call : halt_call + 400]
        assert "return" not in window, "the recovery halt must latch and keep booting, never return"


# ---------------------------------------------------------------------------
# 3. A halted recovery never overwrites the checkpoint it halted on
# ---------------------------------------------------------------------------


class TestCheckpointIsPreservedAcrossARecoveryHalt:
    def test_final_checkpoint_is_skipped_after_a_recovery_halt(self):
        writer = _SpyCheckpointWriter()
        system = _system(checkpoint_writer=writer, _recovery_halted=True)

        system._write_final_checkpoint()

        assert writer.writes == 0

    def test_final_checkpoint_is_written_on_a_normal_shutdown(self):
        writer = _SpyCheckpointWriter()
        system = _system(checkpoint_writer=writer)

        system._write_final_checkpoint()

        assert writer.writes == 1

    def test_final_checkpoint_failure_is_swallowed_so_shutdown_completes(self):
        class _Boom:
            attempts = 0

            def write_checkpoint(self) -> None:
                _Boom.attempts += 1
                raise RuntimeError("disk full")

        system = _system(checkpoint_writer=_Boom())

        system._write_final_checkpoint()  # a failed checkpoint must not abort shutdown

        assert _Boom.attempts == 1

    def test_halt_entry_checkpoint_is_skipped_after_a_recovery_halt(self):
        writer = _SpyCheckpointWriter()
        system = _system(checkpoint_writer=writer, _recovery_halted=True)

        system._write_halt_entry_checkpoint()
        system._write_halt_entry_checkpoint()

        assert writer.writes == 0

    def test_halt_entry_checkpoint_is_written_once_per_halt_episode(self):
        writer = _SpyCheckpointWriter()
        system = _system(checkpoint_writer=writer)

        system._write_halt_entry_checkpoint()
        system._write_halt_entry_checkpoint()

        assert writer.writes == 1
        assert system._halt_checkpoint_written is True

    def test_checkpoint_writer_service_is_not_started_after_a_recovery_halt(self):
        source = SYSTEM_SRC.read_text()
        start = source.index('self._start_service("checkpoint_writer"')
        guard = source[max(0, start - 500) : start]
        assert "self._recovery_halted" in guard, (
            "the periodic checkpoint writer must be suppressed after a recovery halt; "
            "the store is empty there and every write erases the evidence"
        )


# ---------------------------------------------------------------------------
# 4. The latch is read before anything can dispatch
# ---------------------------------------------------------------------------


class TestBootReadsTheLatchBeforeTrading:
    def test_kill_switch_is_read_before_any_order_service_starts(self):
        # Source-level ordering assertion, in the style of
        # test_startup_recovery_ordering.py: driving the whole of run() to
        # observe the order is brittle and invites a false green.
        source = SYSTEM_SRC.read_text()
        boot_check = source.index("kill_switch.is_active()")
        order_service = source.index('self._start_service("order"')
        supervise = source.index("await self._supervise()")

        assert boot_check < order_service, "the boot latch check must precede the order adapter service"
        assert boot_check < supervise, "_supervise() reads the latch far too late to gate a boot"


# ---------------------------------------------------------------------------
# 5. The operator surface and the engine share one latch shape
# ---------------------------------------------------------------------------


class TestOperatorAndEngineAgreeOnTheLatch:
    def test_cli_halt_writes_a_latch_the_engine_reads(self, latch_path, capsys):
        from hft_platform.cli._checks import check_kill_switch
        from hft_platform.cli._risk import cmd_risk_halt, cmd_risk_resume

        class _Args:
            reason = "operator stopping R47"

        cmd_risk_halt(_Args())
        capsys.readouterr()

        assert kill_switch.read_reason() == "operator stopping R47"
        assert check_kill_switch()["ok"] is False

        cmd_risk_resume(_Args())
        capsys.readouterr()

        assert kill_switch.is_active() is False
        assert check_kill_switch()["ok"] is True

    def test_engine_reader_and_latch_module_agree(self, latch_path):
        from hft_platform.services import system as system_module

        kill_switch.activate("STARTUP_POSITION_RECOVERY", actor="startup_position_recovery")

        assert system_module._read_kill_switch_reason(str(latch_path)) == "STARTUP_POSITION_RECOVERY"
        assert os.path.exists(latch_path)

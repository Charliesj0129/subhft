"""B1: the four manual-release stops survive a restart; nothing else is manual.

Policy (2026-10-23): data loss, a reconciliation mismatch, an operator kill and
the daily loss limit need an operator to resume; everything else recovers on
its own. Before this batch the daily-loss stop, the day's realized loss and the
reconciliation hold lived in memory, so a restart released them -- on
2026-10-22 16:44Z a deploy restart cleared a daily-loss HALT in place.

    engine                          disk (.state/)                 operator
    ------                          --------------                 --------
    validator latches DAILY_LOSS -> manual_latches/daily_loss.json
    restart                         (file survives)
    boot: _apply_manual_latches  <- present -> HALT + hold
                                    audit.jsonl  <-  hft risk rearm daily_loss
    next tick: hold released     <- absent
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hft_platform.risk import daily_loss_state, kill_switch, manual_latches
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.risk.validators import DailyLossLimitValidator
from hft_platform.services.system import HFTSystem

PROD_LIMITS = Path(__file__).resolve().parents[2] / "config" / "env" / "prod" / "strategy_limits.yaml"


def _validator() -> DailyLossLimitValidator:
    cfg = {"global_defaults": {}, "intraday_pnl": {"hard_limit_ntd": 15000, "soft_limit_ntd": 500}}
    return DailyLossLimitValidator(cfg)


def _latch_daily(v: DailyLossLimitValidator, reason: str = "DAILY_LOSS_LIMIT") -> None:
    v.halt_triggered = True
    v._halt_reason = reason


class _SpyGauge:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def labels(self, *, latch: str) -> "_SpyGauge":
        self._label = latch
        return self

    def set(self, value: int) -> None:
        self.values[self._label] = value


def _system(validator: DailyLossLimitValidator | None = None) -> HFTSystem:
    system = object.__new__(HFTSystem)
    guard = StormGuard()
    guard.metrics = SimpleNamespace(manual_latch_active=_SpyGauge())
    system.storm_guard = guard
    system.risk_engine = SimpleNamespace(validators=[validator] if validator is not None else [])
    return system


# ---------------------------------------------------------------------------
# The latch store
# ---------------------------------------------------------------------------


class TestManualLatchStore:
    def test_activate_writes_a_latch_that_active_reports(self) -> None:
        assert manual_latches.activate(manual_latches.DAILY_LOSS, "DAILY_LOSS_LIMIT", actor="engine") is True
        assert manual_latches.active() == {"daily_loss": "DAILY_LOSS_LIMIT"}

    def test_activate_keeps_the_first_reason_when_already_latched(self) -> None:
        manual_latches.activate(manual_latches.RECONCILIATION_MISMATCH, "first", actor="engine")
        assert manual_latches.activate(manual_latches.RECONCILIATION_MISMATCH, "second", actor="engine") is False
        assert manual_latches.active() == {"reconciliation_mismatch": "first"}

    def test_unreadable_latch_still_counts_as_active(self) -> None:
        directory = manual_latches.latch_dir()
        directory.mkdir(parents=True)
        (directory / "daily_loss.json").write_text("{torn", encoding="utf-8")
        assert manual_latches.active() == {"daily_loss": "unreadable"}

    def test_release_audits_then_removes(self) -> None:
        manual_latches.activate(manual_latches.DAILY_LOSS, "DAILY_LOSS_LIMIT", actor="engine")
        assert manual_latches.release(manual_latches.DAILY_LOSS, reason="flat, reviewed", actor="cli:op") is True
        assert manual_latches.active() == {}
        audit = (manual_latches.latch_dir() / manual_latches.AUDIT_FILE).read_text(encoding="utf-8").splitlines()
        entry = json.loads(audit[-1])
        assert entry["latch"] == "daily_loss"
        assert entry["reason"] == "flat, reviewed"
        assert entry["actor"] == "cli:op"
        assert entry["removed"]["reason"] == "DAILY_LOSS_LIMIT"

    def test_release_without_a_reason_is_refused_and_keeps_the_latch(self) -> None:
        manual_latches.activate(manual_latches.DAILY_LOSS, "DAILY_LOSS_LIMIT", actor="engine")
        with pytest.raises(ValueError):
            manual_latches.release(manual_latches.DAILY_LOSS, reason="  ", actor="cli:op")
        assert "daily_loss" in manual_latches.active()

    def test_unknown_latch_name_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            manual_latches.activate("crash_loop", "x", actor="engine")

    def test_default_dir_is_under_the_bind_mounted_state_dir(self, monkeypatch) -> None:
        monkeypatch.delenv(manual_latches.DIR_ENV, raising=False)
        assert manual_latches.latch_dir() == Path(".state/manual_latches")


# ---------------------------------------------------------------------------
# Kill switch: durable write, bind-mounted default, legacy path honoured
# ---------------------------------------------------------------------------


class TestKillSwitchDurability:
    def test_default_path_is_under_state(self, monkeypatch) -> None:
        monkeypatch.delenv(kill_switch.PATH_ENV, raising=False)
        assert kill_switch.kill_switch_path() == ".state/kill_switch"

    def test_activate_leaves_no_temp_file_behind(self, tmp_path) -> None:
        target = tmp_path / "ks" / "kill_switch"
        kill_switch.activate("op", actor="cli", path=str(target))
        assert json.loads(target.read_text())["reason"] == "op"
        assert [p.name for p in target.parent.iterdir()] == ["kill_switch"]

    def test_legacy_runtime_latch_is_still_active_and_removed_by_resume(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv(kill_switch.PATH_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        legacy = tmp_path / ".runtime" / "kill_switch"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"reason": "written before the move"}))

        assert kill_switch.active_path() == ".runtime/kill_switch"
        assert kill_switch.read_reason() == "written before the move"
        assert kill_switch.deactivate() is True
        assert not legacy.exists()
        assert kill_switch.is_active() is False


# ---------------------------------------------------------------------------
# StormGuard hold
# ---------------------------------------------------------------------------


class TestManualLatchHold:
    def test_hold_blocks_halt_deescalation(self) -> None:
        guard = StormGuard()
        guard.trigger_halt("MANUAL_LATCH: daily_loss=DAILY_LOSS_LIMIT")
        guard.set_manual_latch_hold(True)
        guard._halt_entry_ts = 0.0
        for _ in range(10):
            guard.update()
        assert guard.state == StormGuardState.HALT

    def test_clearing_the_hold_lets_halt_deescalate(self) -> None:
        guard = StormGuard()
        guard.trigger_halt("MANUAL_LATCH: daily_loss=DAILY_LOSS_LIMIT")
        guard.set_manual_latch_hold(True)
        guard.set_manual_latch_hold(False)
        guard._halt_entry_ts = 0.0
        for _ in range(10):
            guard.update()
        assert guard.state != StormGuardState.HALT

    def test_the_other_holds_do_not_release_it(self) -> None:
        guard = StormGuard()
        guard.set_manual_latch_hold(True)
        guard.set_reconciliation_hold(False)
        guard.set_daily_loss_hold(False)
        guard.set_kill_switch_hold(False)
        assert guard.manual_latch_hold is True


# ---------------------------------------------------------------------------
# Validator state export / restore
# ---------------------------------------------------------------------------


class TestDailyLossStateRoundTrip:
    def test_restore_brings_back_the_days_loss_and_latch(self) -> None:
        before = _validator()
        before.record_pnl("R47", -90_000_000)
        _latch_daily(before)
        before._peak_pnl_scaled = 37_600_000

        after = _validator()
        assert after.restore_state(before.export_state()) is True
        assert after.halt_triggered is True
        assert after.halt_reason == "DAILY_LOSS_LIMIT"
        assert after.export_state()["accumulated"] == {"R47": -90_000_000}
        assert after.export_state()["peak_pnl_scaled"] == 37_600_000

    def test_state_from_another_trading_day_is_ignored(self) -> None:
        before = _validator()
        before.record_pnl("R47", -90_000_000)
        state = before.export_state()
        state["boundary_ns"] -= 86_400 * 1_000_000_000

        after = _validator()
        assert after.restore_state(state) is False
        assert after.export_state()["accumulated"] == {}

    def test_hard_limit_breach_under_a_profit_lock_becomes_a_daily_loss_stop(self) -> None:
        v = _validator()
        _latch_daily(v, "PEAK_DRAWDOWN")
        v.record_pnl("R47", -150_000_000)
        v.update_unrealized(0, complete=True)
        assert v.halt_triggered is True
        assert v.halt_reason == "DAILY_LOSS_LIMIT"

    def test_profit_lock_keeps_its_reason_inside_the_hard_limit(self) -> None:
        v = _validator()
        _latch_daily(v, "PEAK_DRAWDOWN")
        v.update_unrealized(-10_000_000, complete=False)
        assert v.halt_reason == "PEAK_DRAWDOWN"

    def test_malformed_state_raises_and_changes_nothing(self) -> None:
        state = _validator().export_state()
        state["halt_triggered"] = "yes"
        after = _validator()
        with pytest.raises(ValueError):
            after.restore_state(state)
        assert after.halt_triggered is False


# ---------------------------------------------------------------------------
# Engine wiring: latch on the hard limit, persist, restore at boot
# ---------------------------------------------------------------------------


class TestSupervisorSync:
    def test_hard_limit_stop_writes_the_daily_loss_latch_and_halts(self) -> None:
        v = _validator()
        _latch_daily(v)
        system = _system(v)

        asyncio.run(system._sync_manual_latches())

        assert manual_latches.active() == {"daily_loss": "DAILY_LOSS_LIMIT"}
        assert system.storm_guard.state == StormGuardState.HALT
        assert system.storm_guard.manual_latch_hold is True
        assert system.storm_guard.metrics.manual_latch_active.values["daily_loss"] == 1

    def test_peak_drawdown_stop_is_not_a_manual_latch(self) -> None:
        v = _validator()
        _latch_daily(v, "PEAK_DRAWDOWN")
        system = _system(v)

        asyncio.run(system._sync_manual_latches())

        assert manual_latches.active() == {}
        assert system.storm_guard.manual_latch_hold is False

    def test_operator_release_is_not_rewritten_while_the_same_stop_stands(self) -> None:
        v = _validator()
        _latch_daily(v)
        system = _system(v)
        asyncio.run(system._sync_manual_latches())

        manual_latches.release(manual_latches.DAILY_LOSS, reason="reviewed", actor="cli:op")
        asyncio.run(system._sync_manual_latches())

        assert manual_latches.active() == {}
        assert system.storm_guard.manual_latch_hold is False

    def test_latch_outlives_the_validator_flag(self) -> None:
        """The 05:00 roll clears the validator; the file still holds HALT."""
        v = _validator()
        _latch_daily(v)
        system = _system(v)
        asyncio.run(system._sync_manual_latches())

        v.halt_triggered = False
        v._halt_reason = ""
        asyncio.run(system._sync_manual_latches())

        assert manual_latches.active() == {"daily_loss": "DAILY_LOSS_LIMIT"}
        assert system.storm_guard.manual_latch_hold is True

    def test_failed_latch_read_keeps_the_last_known_set(self, monkeypatch) -> None:
        system = _system()
        system._apply_manual_latches({"reconciliation_mismatch": "drift"})

        def _boom(directory=None):
            raise OSError("EIO")

        monkeypatch.setattr(manual_latches, "active", _boom)
        asyncio.run(system._sync_manual_latches())

        assert system.storm_guard.manual_latch_hold is True

    def test_restart_restores_the_days_loss_and_the_latch(self) -> None:
        v = _validator()
        v.record_pnl("R47", -160_000_000)
        _latch_daily(v)
        first = _system(v)
        asyncio.run(first._sync_manual_latches())
        assert daily_loss_state.state_path().exists()

        # New process: fresh validator, fresh StormGuard; only the disk carries over.
        fresh = _validator()
        second = _system(fresh)
        second._restore_daily_loss_state()
        second._apply_manual_latches(manual_latches.active())

        assert fresh.halt_triggered is True
        assert fresh.export_state()["accumulated"] == {"R47": -160_000_000}
        assert second._daily_loss_latch_published is True
        assert second.storm_guard.state == StormGuardState.HALT
        assert second.storm_guard.manual_latch_hold is True

    def test_unreadable_state_file_at_boot_is_ignored(self) -> None:
        path = daily_loss_state.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        fresh = _validator()
        _system(fresh)._restore_daily_loss_state()
        assert fresh.halt_triggered is False

    def test_loss_exit_sees_the_latch_after_the_validator_flag_clears(self) -> None:
        import inspect

        src = inspect.getsource(HFTSystem._run_loss_exit)
        assert "manual_latches.DAILY_LOSS in self._manual_latches_active" in src


# ---------------------------------------------------------------------------
# Reconciliation mismatch
# ---------------------------------------------------------------------------


class TestReconciliationMismatchLatch:
    def test_mismatch_halt_writes_the_latch(self) -> None:
        from hft_platform.execution.reconciliation import ReconciliationService

        service = object.__new__(ReconciliationService)
        service.storm_guard = StormGuard()
        discrepancy = SimpleNamespace(symbol="TMFK6")

        asyncio.run(service._trigger_halt([discrepancy]))

        latches = manual_latches.active()
        assert list(latches) == ["reconciliation_mismatch"]
        assert latches["reconciliation_mismatch"].startswith("RECONCILIATION_MISMATCH: 1 critical")
        assert service.storm_guard.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# CLI and config
# ---------------------------------------------------------------------------


class TestRearmCli:
    def test_rearm_releases_the_named_latch(self, capsys) -> None:
        from hft_platform.cli._risk import cmd_risk_rearm

        manual_latches.activate(manual_latches.DAILY_LOSS, "DAILY_LOSS_LIMIT", actor="engine")
        cmd_risk_rearm(Namespace(latch="daily_loss", reason="flat and reviewed"))

        assert manual_latches.active() == {}
        assert "RELEASED" in capsys.readouterr().out

    def test_parser_accepts_only_known_latches(self) -> None:
        from hft_platform.cli._parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["risk", "rearm", "reconciliation_mismatch", "--reason", "x"])
        assert args.latch == "reconciliation_mismatch"
        with pytest.raises(SystemExit):
            parser.parse_args(["risk", "rearm", "crash_loop", "--reason", "x"])


class TestProdLimits:
    def test_prod_limits_are_15000_daily_and_5000_per_position(self) -> None:
        import yaml

        cfg = yaml.safe_load(PROD_LIMITS.read_text(encoding="utf-8"))
        v = DailyLossLimitValidator(cfg)
        assert v._hard_limit_threshold_scaled == 15_000 * 10_000
        assert v.position_loss_limit_scaled == 5_000 * 10_000
        # The fallback stays above the operative cap (test_daily_loss_limit_precedence).
        assert cfg["global_defaults"]["max_daily_loss"] == 20_000 * 10_000

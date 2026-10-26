"""Operator controls reach the engine and report what really happened (risk redesign B6).

Three controls existed on paper only:

- Telegram ``/stop`` set a Redis key the engine read through a client nothing
  assigned, and the poller that set it was never constructed.
- ``hft ops flatten`` wrote a request only an AutonomyMonitor would read, and
  none was ever given the gate, so the CLI waited 300 s and timed out.
- ``hft risk resume`` and ``hft ops rearm-platform`` released a stop with no
  reason, no record, and no check that the stop was the one the operator read.

Each test drives the operator's entry point and asserts on what the engine side
sees, not on what the command printed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hft_platform.cli._ops import cmd_ops_flatten
from hft_platform.cli._risk import cmd_risk_resume
from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.execution.positions import Position
from hft_platform.ops.flatten_gate import FlattenGate, FlattenStatus
from hft_platform.risk import kill_switch, manual_latches
from hft_platform.risk.loss_exit import LossExitController
from hft_platform.risk.validators import DailyLossLimitValidator, ExpiringContractValidator
from hft_platform.services.system import HFTSystem

SCALE = 10_000
BID, ASK = 46_600 * SCALE, 46_602 * SCALE
NOW = 1_792_000_000_000_000_000


@pytest.fixture(autouse=True)
def _isolated_latches(tmp_path, monkeypatch):
    monkeypatch.setenv(kill_switch.PATH_ENV, str(tmp_path / ".state" / "kill_switch"))
    monkeypatch.setenv(manual_latches.DIR_ENV, str(tmp_path / ".state" / "manual_latches"))


# ---------------------------------------------------------------------------
# Telegram /stop
# ---------------------------------------------------------------------------


def _update(chat_id: int):
    replies: list[str] = []

    async def reply_text(text):
        replies.append(text)

    update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=SimpleNamespace(reply_text=reply_text))
    return update, replies


@pytest.fixture
def owner(monkeypatch):
    import hft_platform.bot.app as bot_app

    monkeypatch.setattr(bot_app, "_OWNER_CHAT_ID", 4242)
    return 4242


class TestTelegramStop:
    def test_stop_writes_the_latch_the_engine_reads(self, owner):
        from hft_platform.bot.handlers import cmd_stop
        from hft_platform.services import system as system_module

        update, replies = _update(owner)
        asyncio.run(cmd_stop(update, SimpleNamespace(args=["spread", "blew", "out"])))

        path = kill_switch.active_path()
        assert path is not None
        # The engine's supervise tick reads the reason through this helper.
        assert system_module._read_kill_switch_reason(path) == "TELEGRAM: spread blew out"
        assert kill_switch.read_payload()["actor"] == "telegram"
        [reply] = replies
        assert f"--token {kill_switch.current_token()}" in reply
        assert "對帳與券商一致才自動平倉" in reply

    def test_the_token_in_the_reply_releases_it(self, owner, capsys):
        from argparse import Namespace

        from hft_platform.bot.handlers import cmd_stop

        update, replies = _update(owner)
        asyncio.run(cmd_stop(update, SimpleNamespace(args=[])))
        token = replies[0].split("--token ")[1].split()[0]

        cmd_risk_resume(Namespace(token=token, reason="stop was a drill"))

        assert kill_switch.is_active() is False

    def test_a_stranger_cannot_stop_the_engine(self, owner):
        from hft_platform.bot.handlers import cmd_stop

        update, replies = _update(owner + 1)
        asyncio.run(cmd_stop(update, SimpleNamespace(args=[])))

        assert kill_switch.is_active() is False
        assert replies == ["未授權"]

    def test_a_failed_write_is_reported_as_a_failure(self, owner):
        from hft_platform.bot.handlers import cmd_stop

        update, replies = _update(owner)
        with mock.patch.object(kill_switch, "activate", side_effect=PermissionError("read-only")):
            asyncio.run(cmd_stop(update, SimpleNamespace(args=[])))

        [reply] = replies
        assert reply.startswith("❌")
        assert "hft risk halt" in reply

    def test_stop_is_registered_on_the_bot(self, monkeypatch):
        """python-telegram-bot is not a test dependency; a stand-in records the handlers."""
        registered: list[str] = []

        class _CommandHandler:
            def __init__(self, name, _fn):
                registered.append(name)

        class _App:
            job_queue = None

            def add_handler(self, _h):
                pass

            def add_error_handler(self, _h):
                pass

        builder = SimpleNamespace(token=lambda _t: SimpleNamespace(build=lambda: _App()))
        ext = types.ModuleType("telegram.ext")
        ext.Application = SimpleNamespace(builder=lambda: builder)
        ext.CommandHandler = _CommandHandler
        monkeypatch.setitem(sys.modules, "telegram", types.ModuleType("telegram"))
        monkeypatch.setitem(sys.modules, "telegram.ext", ext)
        monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "x")

        from hft_platform.bot import app as bot_app

        with mock.patch("hft_platform.bot.scheduler.schedule_jobs"):
            bot_app.create_app()

        assert "stop" in registered


# ---------------------------------------------------------------------------
# Releases quote the token of the stop they clear
# ---------------------------------------------------------------------------


class TestReleaseNamesTheStop:
    def test_a_halt_written_after_the_operator_read_status_is_not_released(self):
        kill_switch.activate("operator drill", actor="cli")
        token_read = kill_switch.current_token()
        kill_switch.activate("TELEGRAM: real problem", actor="telegram")

        with pytest.raises(ValueError, match="does not match"):
            kill_switch.release(token=token_read, reason="drill over", actor="cli:op")

        assert kill_switch.read_reason() == "TELEGRAM: real problem"

    def test_a_release_leaves_an_audit_line_before_the_file_goes(self):
        kill_switch.activate("operator drill", actor="cli")
        kill_switch.release(token=kill_switch.current_token(), reason="drill over", actor="cli:op")

        audit = Path(kill_switch.kill_switch_path()).with_name(kill_switch.AUDIT_FILE)
        [entry] = [json.loads(line) for line in audit.read_text().splitlines()]
        assert (entry["actor"], entry["reason"]) == ("cli:op", "drill over")
        assert list(entry["removed"].values())[0]["reason"] == "operator drill"
        assert kill_switch.is_active() is False


# ---------------------------------------------------------------------------
# hft ops flatten
# ---------------------------------------------------------------------------


class _Queue:
    def __init__(self) -> None:
        self.items = []

    def submit_nowait(self, item) -> None:
        self.items.append(item)


def _pos(strategy_id: str, symbol: str, net_qty: int) -> Position:
    return Position(
        account_id="acc", strategy_id=strategy_id, symbol=symbol, net_qty=net_qty, avg_price_scaled=46_000 * SCALE
    )


def _system(gate: FlattenGate, positions: dict[str, Position], *, loss_exit_enabled: bool = True) -> HFTSystem:
    cfg = {"intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000}, "loss_exit": {"enabled": loss_exit_enabled}}
    system = HFTSystem.__new__(HFTSystem)
    system._loss_exit = LossExitController()
    system._flatten_gate = gate
    system.risk_engine = SimpleNamespace(
        config=cfg, validators=[DailyLossLimitValidator(cfg, None), ExpiringContractValidator(cfg, None)]
    )
    l1 = (NOW, BID, ASK, BID + ASK, ASK - BID, 3, 3)
    system.md_service = SimpleNamespace(lob=SimpleNamespace(get_l1_scaled=lambda _s: l1))
    system.position_store = SimpleNamespace(
        snapshot_open_positions=lambda: dict(positions),
        metadata=SimpleNamespace(contract_multiplier=lambda _s: 10, tick_size_scaled=lambda _s: SCALE),
    )
    system.risk_queue = _Queue()
    return system


def _tick(system: HFTSystem, now_ns: int = NOW) -> None:
    """One supervise-tick's worth of the flatten path: sync the request, then run the exit."""
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=now_ns):
        asyncio.run(system._sync_operator_flatten())
        system._run_loss_exit()


@pytest.fixture
def gate(tmp_path) -> FlattenGate:
    return FlattenGate(path=tmp_path / "flatten_request.json")


class TestOperatorFlatten:
    def test_flatten_sends_a_force_flat_and_completes_when_the_position_is_flat(self, gate):
        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 2)}
        system = _system(gate, positions)
        gate.submit(scope="all", deadline_s=60)

        _tick(system)
        [intent] = system.risk_queue.items
        assert (intent.intent_type, intent.side, intent.symbol) == (IntentType.FORCE_FLAT, Side.SELL, "TMFK6")
        assert intent.reason == "loss_exit:operator_flatten"
        assert gate.read_request().status == FlattenStatus.PROCESSING

        positions.clear()  # the IOC filled
        _tick(system)

        req = gate.read_request()
        assert (req.status, req.fully_closed, req.failed, req.failed_symbols) == (FlattenStatus.COMPLETED, 1, 0, [])

    def test_a_position_still_open_at_the_deadline_is_reported_open(self, gate):
        positions = {"a": _pos("R47_MAKER_TMF", "TMFK6", 2), "b": _pos("R47_MAKER_TMF", "TMFL6", -1)}
        system = _system(gate, positions)
        gate.submit(scope="all", deadline_s=5)
        _tick(system)

        positions["a"] = _pos("R47_MAKER_TMF", "TMFK6", 1)  # one of two lots filled
        _tick(system, NOW + 6_000_000_000)

        req = gate.read_request()
        assert req.status == FlattenStatus.COMPLETED
        assert (req.fully_closed, req.partially_closed, req.failed) == (0, 1, 1)
        assert req.failed_symbols == ["TMFK6", "TMFL6"]

    def test_a_newer_request_is_not_overwritten_with_the_older_result(self, gate):
        positions = {"a": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        gate.submit(scope="all", deadline_s=60)
        _tick(system)

        with mock.patch("hft_platform.ops.flatten_gate.time.monotonic_ns", return_value=123):
            gate.submit(scope="strategy", scope_id="R47_MAKER_TMF", deadline_s=60)
        positions.clear()
        _tick(system)  # the first request finishes; the file holds the second

        req = gate.read_request()
        assert (req.status, req.scope, req.initiated_ns) == (FlattenStatus.PENDING, "strategy", 123)
        _tick(system)
        assert gate.read_request().status == FlattenStatus.COMPLETED

    def test_the_cli_exits_non_zero_when_the_engine_reports_a_symbol_open(self, gate, capsys):
        gate.submit(scope="all", deadline_s=5)
        gate.claim()
        gate.complete(0, 0, 1, ["TMFK6"])
        with mock.patch("hft_platform.cli._ops._flatten_via_gate", return_value=gate.read_request()):
            with pytest.raises(SystemExit) as exc:
                cmd_ops_flatten(SimpleNamespace(scope="all", scope_id=None, deadline=5))
        assert exc.value.code == 1
        assert "Still open: TMFK6" in capsys.readouterr().out

    def test_nothing_open_completes_at_once(self, gate):
        system = _system(gate, {})
        gate.submit(scope="all", deadline_s=60)
        _tick(system)

        req = gate.read_request()
        assert (req.status, req.fully_closed, req.failed) == (FlattenStatus.COMPLETED, 0, 0)
        assert system.risk_queue.items == []

    def test_strategy_scope_closes_only_that_strategys_symbols(self, gate):
        positions = {"a": _pos("R47_MAKER_TMF", "TMFK6", 1), "b": _pos("OTHER", "TXFK6", 1)}
        system = _system(gate, positions)
        gate.submit(scope="strategy", scope_id="R47_MAKER_TMF", deadline_s=60)
        _tick(system)

        assert [i.symbol for i in system.risk_queue.items] == ["TMFK6"]

    def test_it_runs_with_the_automatic_loss_exit_switched_off(self, gate):
        system = _system(gate, {"a": _pos("R47_MAKER_TMF", "TMFK6", 1)}, loss_exit_enabled=False)
        gate.submit(scope="all", deadline_s=60)
        _tick(system)

        [intent] = system.risk_queue.items
        assert intent.reason == "loss_exit:operator_flatten"

    def test_a_request_left_processing_by_a_dead_engine_is_failed_not_left_waiting(self, gate):
        gate.submit(scope="all", deadline_s=60)
        gate.claim()  # the previous process claimed it and died
        system = _system(gate, {"a": _pos("R47_MAKER_TMF", "TMFK6", 1)})

        _tick(system)

        req = gate.read_request()
        assert req.status == FlattenStatus.FAILED
        assert "restarted" in (req.error or "")
        assert system.risk_queue.items == []

    def test_an_unknown_scope_fails_the_request(self, gate):
        system = _system(gate, {"a": _pos("R47_MAKER_TMF", "TMFK6", 1)})
        gate.submit(scope="track", scope_id="day", deadline_s=60)
        _tick(system)

        req = gate.read_request()
        assert req.status == FlattenStatus.FAILED
        assert system.risk_queue.items == []


def _trade(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(contract=SimpleNamespace(code=symbol))


class TestFlattenCancelsWorkingOrders:
    """A working order in a flattened symbol can fill after the book is flat and reopen it."""

    def test_a_working_order_is_cancelled_and_holds_completion_until_it_is_gone(self, gate):
        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 2)}
        system = _system(gate, positions)
        live = {"R47_MAKER_TMF:17": _trade("TMFK6"), "R47_MAKER_TMF:18": _trade("TXFK6")}
        system.order_adapter = SimpleNamespace(live_orders=live)
        gate.submit(scope="all", deadline_s=60)

        _tick(system)
        cancels = [i for i in system.risk_queue.items if i.intent_type == IntentType.CANCEL]
        assert [(c.target_order_id, c.symbol) for c in cancels] == [("R47_MAKER_TMF:17", "TMFK6")]
        assert cancels[0].reason == "loss_exit:operator_flatten:cancel"

        positions.clear()  # the IOC filled; the quote is still resting
        _tick(system)
        assert gate.read_request().status == FlattenStatus.PROCESSING

        del live["R47_MAKER_TMF:17"]  # the broker confirmed the cancel
        _tick(system)
        req = gate.read_request()
        assert (req.status, req.fully_closed, req.failed_symbols) == (FlattenStatus.COMPLETED, 1, [])

    def test_an_order_not_yet_acknowledged_holds_completion(self, gate):
        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        live: dict = {"R47_MAKER_TMF:19": object()}  # sent, no contract known yet
        system.order_adapter = SimpleNamespace(live_orders=live)
        gate.submit(scope="all", deadline_s=60)
        _tick(system)

        positions.clear()
        _tick(system)
        assert gate.read_request().status == FlattenStatus.PROCESSING

        live.clear()
        _tick(system)
        assert gate.read_request().status == FlattenStatus.COMPLETED

    def test_a_working_order_left_at_the_deadline_is_reported_open(self, gate):
        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        system.order_adapter = SimpleNamespace(live_orders={"R47_MAKER_TMF:17": _trade("TMFK6")})
        gate.submit(scope="all", deadline_s=5)
        _tick(system)

        positions.clear()
        _tick(system, NOW + 6_000_000_000)

        req = gate.read_request()
        assert req.status == FlattenStatus.COMPLETED
        assert (req.fully_closed, req.failed_symbols) == (0, ["TMFK6"])

    def test_the_platforms_own_exit_is_neither_cancelled_nor_waited_for(self, gate):
        from hft_platform.risk.loss_exit import INTENT_ID_BASE

        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        exit_key = f"R47_MAKER_TMF:{INTENT_ID_BASE + 1}"
        system.order_adapter = SimpleNamespace(live_orders={exit_key: _trade("TMFK6")})
        gate.submit(scope="all", deadline_s=60)
        _tick(system)
        assert not [i for i in system.risk_queue.items if i.intent_type == IntentType.CANCEL]

        positions.clear()
        _tick(system)
        assert gate.read_request().status == FlattenStatus.COMPLETED


# ---------------------------------------------------------------------------
# A release and a new stop cannot interleave
# ---------------------------------------------------------------------------


def _during_audit(module, write):
    """Patch ``release_audit.append`` (between a release's check and its removal)
    to start ``write`` on another thread, as a second process would.

    The 50 ms join bounds how long an unserialized write needs to land; with
    the lock it is still blocked when the join gives up.
    """
    started: list[threading.Thread] = []
    real_append = module.release_audit.append

    def _append(*args, **kwargs):
        real_append(*args, **kwargs)
        t = threading.Thread(target=write)
        t.start()
        t.join(timeout=0.05)
        started.append(t)

    return started, mock.patch.object(module.release_audit, "append", side_effect=_append)


class TestReleaseIsSerializedWithNewStops:
    def test_a_kill_switch_written_during_a_release_survives_it(self):
        kill_switch.activate("operator drill", actor="cli")
        started, patch = _during_audit(kill_switch, lambda: kill_switch.activate("TELEGRAM: real", actor="telegram"))
        with patch:
            kill_switch.release(token=kill_switch.current_token(), reason="drill over", actor="cli:op")
        started[0].join()

        assert kill_switch.read_reason() == "TELEGRAM: real"

    def test_a_manual_latch_raised_during_its_release_survives_it(self):
        name = manual_latches.RECONCILIATION_MISMATCH
        manual_latches.activate(name, "first mismatch", actor="engine")
        token = manual_latches.tokens()[name]
        started, patch = _during_audit(
            manual_latches, lambda: manual_latches.activate(name, "second mismatch", actor="engine")
        )
        with patch:
            manual_latches.release(name, token=token, reason="books agree", actor="cli:op")
        started[0].join()

        assert manual_latches.active() == {name: "second mismatch"}

    def test_a_platform_degradation_recorded_during_a_rearm_survives_it(self, tmp_path):
        from hft_platform.ops import manual_rearm
        from hft_platform.ops.runtime_state_store import locked_state

        path = tmp_path / "runtime_state.json"
        with locked_state(path) as state:
            state["platform"] = {"manual_rearm_required": True, "reason": "wal_data_loss"}
        gate_ = manual_rearm.ManualRearmGate(state_path=path)

        def _degrade() -> None:
            with locked_state(path) as state:
                state["platform"] = {"manual_rearm_required": True, "reason": "disk_full"}

        started, patch = _during_audit(manual_rearm, _degrade)
        with patch:
            gate_.rearm_platform(token=gate_.platform_token(), reason="wal replayed", actor="cli:op")
        started[0].join()

        assert gate_.requires_manual_rearm("platform") is True
        assert gate_.snapshot()["platform"]["reason"] == "disk_full"


# ---------------------------------------------------------------------------
# Review follow-ups (Codex adversarial review of B0-B6, 2026-10-24)
# ---------------------------------------------------------------------------


class TestPlatformTokenNamesOneIncident:
    def test_a_stale_rearm_does_not_clear_a_second_latch_with_the_same_reason(self, tmp_path):
        from hft_platform.ops import manual_rearm
        from hft_platform.ops.evidence import AutonomyEvidenceWriter

        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        gate_ = manual_rearm.ManualRearmGate(state_path=tmp_path / "runtime_state.json")

        def _latch() -> None:
            writer.record_transition(
                scope="platform", mode="platform_reduce_only", reason="recorder_data_loss", manual_rearm_required=True
            )

        _latch()
        first_token = gate_.platform_token()
        gate_.rearm_platform(token=first_token, reason="wal replayed", actor="cli:op")
        _latch()  # the same failure again, a second incident

        with pytest.raises(ValueError, match="does not match"):
            gate_.rearm_platform(token=first_token, reason="retried command", actor="cli:op")
        assert gate_.requires_manual_rearm("platform") is True

    def test_re_recording_the_same_latch_keeps_its_token(self, tmp_path):
        from hft_platform.ops import manual_rearm
        from hft_platform.ops.evidence import AutonomyEvidenceWriter

        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        gate_ = manual_rearm.ManualRearmGate(state_path=tmp_path / "runtime_state.json")
        for _ in range(2):
            writer.record_transition(
                scope="platform", mode="platform_reduce_only", reason="disk_full", manual_rearm_required=True
            )
            token = gate_.platform_token() if _ == 0 else token

        assert gate_.platform_token() == token


class TestFlattenOutcomeIsHonest:
    def test_an_order_still_unacknowledged_at_the_deadline_fails_the_request(self, gate):
        positions = {"k": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        system.order_adapter = SimpleNamespace(live_orders={"R47_MAKER_TMF:19": object()})
        gate.submit(scope="all", deadline_s=5)
        _tick(system)

        positions.clear()
        _tick(system, NOW + 6_000_000_000)

        req = gate.read_request()
        assert req.status == FlattenStatus.FAILED
        assert "acknowledg" in (req.error or "")

    def test_the_cli_exits_non_zero_on_a_failed_request(self, gate, capsys):
        gate.submit(scope="all", deadline_s=5)
        gate.claim()
        gate.fail("orders awaiting acknowledgement")
        with mock.patch("hft_platform.cli._ops._flatten_via_gate", return_value=gate.read_request()):
            with pytest.raises(SystemExit) as exc:
                cmd_ops_flatten(SimpleNamespace(scope="all", scope_id=None, deadline=5))
        assert exc.value.code != 0

    def test_a_result_is_not_written_onto_a_request_submitted_after_the_check(self, gate):
        positions = {"a": _pos("R47_MAKER_TMF", "TMFK6", 1)}
        system = _system(gate, positions)
        gate.submit(scope="all", deadline_s=60)
        _tick(system)
        positions.clear()

        # The CLI (another process: a thread here) submits request B just after
        # the engine has read request A to answer it. The 50 ms join bounds how
        # long an unserialized submit needs to land; under the lock it waits.
        real_read = FlattenGate.read_request
        started: list[threading.Thread] = []

        def _submit_b() -> None:
            with mock.patch("hft_platform.ops.flatten_gate.time.monotonic_ns", return_value=202):
                gate.submit(scope="strategy", scope_id="R47_MAKER_TMF", deadline_s=60)

        def _read(self):
            current = real_read(self)
            if not started:
                t = threading.Thread(target=_submit_b)
                t.start()
                t.join(timeout=0.05)
                started.append(t)
            return current

        with mock.patch.object(FlattenGate, "read_request", _read):
            with mock.patch("hft_platform.core.timebase.now_ns", return_value=NOW):
                asyncio.run(system._sync_operator_flatten())
        started[0].join()

        req = gate.read_request()
        assert (req.status, req.initiated_ns) == (FlattenStatus.PENDING, 202)

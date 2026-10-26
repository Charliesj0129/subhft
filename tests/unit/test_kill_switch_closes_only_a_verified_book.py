"""The kill switch cancels resting orders and closes the book only when the book is verified.

Agreed 2026-10-24: a kill switch stops trading and cancels what rests at the
exchange. It closes positions itself only when the last reconciliation, at most
``max(15 s, 3 sync intervals)`` old, found the platform's positions equal to
the broker's; otherwise it sends nothing and raises
``kill_switch_flatten_blocked``, because a closing order computed from a wrong
book can open a position instead of closing one. For the same reason the
switch the startup recovery gate writes never closes anything: only a switch a
person set (``hft risk halt``, Telegram ``/stop``) does.

    kill_switch_hold ──> _run_loss_exit
                           ├─ cancel_intents(live_orders)          always
                           └─ set by an operator and verified recently?
                                yes -> FORCE_FLAT (loss_exit:kill_switch)
                                no  -> gauge = 1, nothing sent
"""

from __future__ import annotations

import json
import time
from types import MappingProxyType, SimpleNamespace
from unittest import mock

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.execution.positions import Position
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.loss_exit import INTENT_ID_BASE, LossExitController, OpenPosition
from hft_platform.risk.validators import DailyLossLimitValidator, ExpiringContractValidator
from hft_platform.services.system import HFTSystem, _read_kill_switch_actor

SCALE = 10_000
BID, ASK = 46_600 * SCALE, 46_602 * SCALE
NOW = 1_792_000_000_000_000_000


class _Queue:
    def __init__(self) -> None:
        self.items: list = []

    def submit_nowait(self, item) -> None:
        self.items.append(item)


def _trade(code: str) -> SimpleNamespace:
    return SimpleNamespace(contract=SimpleNamespace(code=code))


def _system(
    *,
    kill: bool,
    verified_age_s: float | None,
    positions: dict | None = None,
    live: dict | None = None,
    actor: str = "cli",
):
    cfg = {"intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000}}
    system = HFTSystem.__new__(HFTSystem)
    system._kill_switch_actor = actor
    system._loss_exit = LossExitController()
    system.storm_guard = SimpleNamespace(kill_switch_hold=kill)
    system.risk_engine = SimpleNamespace(
        config=cfg, validators=[DailyLossLimitValidator(cfg, None), ExpiringContractValidator(cfg, None)]
    )
    l1 = (NOW, BID, ASK, BID + ASK, ASK - BID, 3, 3)
    system.md_service = SimpleNamespace(lob=SimpleNamespace(get_l1_scaled=lambda _s: l1))
    if positions is None:
        positions = {
            "k": Position(
                account_id="acc",
                strategy_id="R47_MAKER_TMF",
                symbol="TMFK6",
                net_qty=1,
                avg_price_scaled=46_000 * SCALE,
            )
        }
    system.position_store = SimpleNamespace(
        snapshot_open_positions=lambda: dict(positions),
        metadata=SimpleNamespace(contract_multiplier=lambda _s: 10, tick_size_scaled=lambda _s: SCALE),
    )
    verified_ns = 0 if verified_age_s is None else time.monotonic_ns() - int(verified_age_s * 1e9)
    system.recon_service = SimpleNamespace(positions_verified_mono_ns=verified_ns, check_interval_s=5.0)
    system.order_adapter = SimpleNamespace(live_orders=live if live is not None else {})
    system.risk_queue = _Queue()
    return system


def _run(system: HFTSystem) -> list:
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=NOW):
        system._run_loss_exit()
    return system.risk_queue.items


def _blocked() -> float:
    return MetricsRegistry.get().kill_switch_flatten_blocked._value.get()


def test_a_verified_book_is_closed_and_resting_orders_cancelled() -> None:
    live = {"R47_MAKER_TMF:41": _trade("TMFK6")}
    items = _run(_system(kill=True, verified_age_s=3, live=live))

    assert {(i.intent_type, i.reason) for i in items} == {
        (IntentType.FORCE_FLAT, "loss_exit:kill_switch"),
        (IntentType.CANCEL, "loss_exit:kill_switch:cancel"),
    }
    assert len(items) == 2
    [cancel] = [i for i in items if i.intent_type == IntentType.CANCEL]
    assert (cancel.target_order_id, cancel.strategy_id, cancel.symbol) == ("R47_MAKER_TMF:41", "R47_MAKER_TMF", "TMFK6")
    assert _blocked() == 0


@pytest.mark.parametrize("age_s", [None, 60.0])
def test_an_unverified_or_stale_book_is_not_closed_and_says_so(age_s) -> None:
    live = {"R47_MAKER_TMF:41": _trade("TMFK6")}
    items = _run(_system(kill=True, verified_age_s=age_s, live=live))

    assert [i.intent_type for i in items] == [IntentType.CANCEL], "cancels still go; no closing order"
    assert _blocked() == 1


def test_the_gauge_returns_to_zero_when_the_switch_is_released() -> None:
    system = _system(kill=True, verified_age_s=None)
    _run(system)
    assert _blocked() == 1

    system.storm_guard = SimpleNamespace(kill_switch_hold=False)
    system.risk_queue.items.clear()
    assert _run(system) == []
    assert _blocked() == 0


def test_a_flat_book_is_not_reported_blocked() -> None:
    _run(_system(kill=True, verified_age_s=None, positions={}))

    assert _blocked() == 0


def test_without_the_switch_nothing_is_cancelled() -> None:
    live = {"R47_MAKER_TMF:41": _trade("TMFK6")}

    assert _run(_system(kill=False, verified_age_s=3, live=live)) == []


def test_an_order_still_waiting_for_the_broker_is_taken_on_a_later_tick() -> None:
    live = {"R47_MAKER_TMF:42": object()}  # pending sentinel: no contract yet

    items = _run(_system(kill=True, verified_age_s=None, live=live))

    assert items == []


@pytest.mark.parametrize("actor", ["cli", "telegram"])
def test_an_operator_switch_closes_a_verified_book(actor) -> None:
    items = _run(_system(kill=True, verified_age_s=3, actor=actor))

    assert [i.reason for i in items] == ["loss_exit:kill_switch"]


@pytest.mark.parametrize("actor", ["startup_position_recovery", "unreadable", ""])
def test_a_switch_no_operator_set_cancels_but_never_closes(actor) -> None:
    live = {"R47_MAKER_TMF:41": _trade("TMFK6")}

    items = _run(_system(kill=True, verified_age_s=3, live=live, actor=actor))

    assert [i.intent_type for i in items] == [IntentType.CANCEL]
    assert _blocked() == 1


def test_a_kill_switch_during_an_operator_flatten_does_not_resend_cancels_every_tick() -> None:
    """One cancel batch per tick: a second, narrower batch would drop the first's retry clock."""
    live = {"R47_MAKER_TMF:41": _trade("TMFK6"), "R47_MAKER_TMF:42": _trade("TMFH6")}
    system = _system(kill=True, verified_age_s=3, live=live)
    system._operator_flatten_initial = MappingProxyType({"TMFK6": 1})

    def _cancels_at(now_ns: int) -> list:
        system.risk_queue.items.clear()
        with mock.patch("hft_platform.core.timebase.now_ns", return_value=now_ns):
            system._run_loss_exit()
        return [i for i in system.risk_queue.items if i.intent_type == IntentType.CANCEL]

    first = _cancels_at(NOW)
    assert sorted(i.target_order_id for i in first) == ["R47_MAKER_TMF:41", "R47_MAKER_TMF:42"]
    assert {i.reason for i in first} == {"loss_exit:kill_switch:cancel"}
    assert _cancels_at(NOW + 1_000_000_000) == []


def test_a_recovery_halt_never_closes_even_after_an_operator_stop() -> None:
    """A /stop written over the recovery latch: the store is still the untrusted one."""
    system = _system(kill=True, verified_age_s=3, actor="telegram")
    system._recovery_halted = True

    assert _run(system) == []
    assert _blocked() == 1


class TestReadKillSwitchActor:
    def test_the_recorded_actor_is_returned(self, tmp_path) -> None:
        path = tmp_path / "kill_switch.json"
        path.write_text(json.dumps({"reason": "r", "timestamp_ns": 1, "actor": "telegram"}))

        assert _read_kill_switch_actor(str(path)) == "telegram"

    def test_a_record_without_an_actor_reads_empty(self, tmp_path) -> None:
        path = tmp_path / "kill_switch.json"
        path.write_text(json.dumps({"reason": "r"}))

        assert _read_kill_switch_actor(str(path)) == ""

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_an_unreadable_record_reads_unreadable(self, tmp_path, content) -> None:
        path = tmp_path / "kill_switch.json"
        path.write_text(content)

        assert _read_kill_switch_actor(str(path)) == "unreadable"


def test_switching_the_automatic_exit_off_also_stops_the_kill_switch_orders() -> None:
    system = _system(kill=True, verified_age_s=3, live={"R47_MAKER_TMF:41": _trade("TMFK6")})
    system.risk_engine.config["loss_exit"] = {"enabled": False}

    assert _run(system) == []


class TestCancelIntents:
    def test_the_platforms_own_exits_are_not_cancelled(self) -> None:
        c = LossExitController()
        orders = [
            ("R47_MAKER_TMF:41", "R47_MAKER_TMF", "TMFK6"),
            (f"R47_MAKER_TMF:{INTENT_ID_BASE + 3}", "R47_MAKER_TMF", "TMFK6"),
        ]

        assert [i.target_order_id for i in c.cancel_intents(orders, NOW)] == ["R47_MAKER_TMF:41"]

    def test_a_cancel_is_retried_only_after_the_retry_interval(self) -> None:
        c = LossExitController(retry_interval_s=10)
        orders = [("R47_MAKER_TMF:41", "R47_MAKER_TMF", "TMFK6")]

        assert len(c.cancel_intents(orders, NOW)) == 1
        assert c.cancel_intents(orders, NOW + 5_000_000_000) == []
        assert len(c.cancel_intents(orders, NOW + 11_000_000_000)) == 1

    def test_planning_exits_does_not_reset_the_cancel_retry_clock(self) -> None:
        c = LossExitController(retry_interval_s=10)
        orders = [("R47_MAKER_TMF:41", "R47_MAKER_TMF", "TMFK6")]
        c.cancel_intents(orders, NOW)

        c.plan(
            now_ns=NOW + 1,
            positions=[OpenPosition("R47_MAKER_TMF", "TMFK6", 0, 0)],
            daily_halt=False,
            position_loss_limit_scaled=0,
            expiry_enabled=False,
            l1=lambda _s: None,
            multiplier=lambda _s: 10,
            tick_size=lambda _s: SCALE,
        )

        assert c.cancel_intents(orders, NOW + 2) == []

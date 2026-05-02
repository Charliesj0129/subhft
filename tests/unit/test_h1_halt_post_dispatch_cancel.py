"""H1: HALT TOCTOU on broker dispatch.

The 395ms Shioaji P95 ``place_order`` await is wide enough that StormGuard
can transition NORMAL → HALT mid-dispatch. The pre-check in ``_api_worker``
sees NORMAL, the broker accepts the order, the OrderAdapter post-await
sees HALT — but the order is already live at the broker. This test
verifies:

1. ``StormGuard.begin_dispatch`` issues a ticket atomically with the
   validation, ``end_dispatch`` reports whether HALT was triggered while
   the ticket was outstanding.
2. ``OrderAdapter._end_dispatch_ticket`` issues a defensive cancel via
   ``client.cancel_order`` (NOT ``place_order`` — Constitution allows
   cancels during HALT) when ``end_dispatch`` reports halted=True.
3. ``order_halt_post_dispatch_cancel_total`` increments only on the
   TOCTOU path, not on ordinary HALT pre-skip.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuard


@pytest.fixture
def tmp_config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rate_limits: {}\ncircuit_breaker: {}\n")
    return str(cfg)


def _intent(intent_id: int = 1, intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="S1",
        symbol="TMFD6",
        intent_type=intent_type,
        side=Side.BUY,
        price=10000,
        qty=1,
    )


class TestStormGuardDispatchTickets:
    def test_begin_dispatch_returns_ticket_when_normal(self):
        sg = StormGuard()
        ok, _, ticket_id = sg.begin_dispatch(_intent())
        assert ok is True
        assert ticket_id is not None

    def test_begin_dispatch_rejects_when_halt(self):
        sg = StormGuard()
        sg.trigger_halt("test")
        ok, reason, ticket_id = sg.begin_dispatch(_intent())
        assert ok is False
        assert reason == "STORMGUARD_HALT"
        assert ticket_id is None

    def test_end_dispatch_no_halt_returns_false(self):
        sg = StormGuard()
        _ok, _r, ticket_id = sg.begin_dispatch(_intent())
        assert sg.end_dispatch(ticket_id) is False

    def test_end_dispatch_after_halt_returns_true(self):
        """Begin in NORMAL, HALT triggers, end_dispatch reports halted=True."""
        sg = StormGuard()
        _ok, _r, ticket_id = sg.begin_dispatch(_intent())
        sg.trigger_halt("test_mid_dispatch")
        assert sg.end_dispatch(ticket_id) is True

    def test_end_dispatch_idempotent_unknown_ticket(self):
        sg = StormGuard()
        # Never issued — must not raise.
        assert sg.end_dispatch(99999) is False

    def test_trigger_halt_marks_all_outstanding_tickets(self):
        """Multiple in-flight dispatches all see halted=True post-HALT."""
        sg = StormGuard()
        _, _, t1 = sg.begin_dispatch(_intent(1))
        _, _, t2 = sg.begin_dispatch(_intent(2))
        _, _, t3 = sg.begin_dispatch(_intent(3))
        sg.trigger_halt("multi-marker")
        assert sg.end_dispatch(t1) is True
        assert sg.end_dispatch(t2) is True
        assert sg.end_dispatch(t3) is True

    def test_concurrent_halt_and_end_dispatch_serialised(self):
        """Multi-thread stress: writers begin/end while another thread halts.
        No deadlocks, no exceptions, every ticket is properly cleaned up."""
        sg = StormGuard()
        stop = threading.Event()
        errors: list[BaseException] = []

        def dispatcher() -> None:
            try:
                while not stop.is_set():
                    _, _, ticket = sg.begin_dispatch(_intent())
                    if ticket is not None:
                        sg.end_dispatch(ticket)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def halter() -> None:
            try:
                i = 0
                while not stop.is_set():
                    if i % 2 == 0:
                        sg.trigger_halt("storm-stress")
                    else:
                        sg.state = StormGuardState.NORMAL
                    i += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        d = threading.Thread(target=dispatcher, daemon=True)
        h = threading.Thread(target=halter, daemon=True)
        d.start()
        h.start()
        try:
            stop.wait(0.3)
        finally:
            stop.set()
            d.join(timeout=2)
            h.join(timeout=2)
        assert not errors, f"concurrent dispatch raised: {errors[0]!r}"
        # All begin/end pairs must clear the inflight dict eventually.
        # (Some may still be in-flight at stop time; allow small slack.)
        assert len(sg._inflight_dispatch_tickets) <= 1


@pytest.mark.asyncio
class TestOrderAdapterPostDispatchCancel:
    def _make_adapter(self, tmp_config: str) -> OrderAdapter:
        client = MagicMock()
        client.place_order = MagicMock(return_value=MagicMock(name="trade_obj"))
        client.cancel_order = MagicMock(return_value=MagicMock())
        client.update_order = MagicMock(return_value=MagicMock())
        client.mode = "simulation"
        client.activate_ca = False
        adapter = OrderAdapter(
            config_path=tmp_config,
            order_queue=asyncio.Queue(),
            broker_client=client,
        )
        adapter.shadow_sink.enabled = False
        return adapter

    async def test_no_storm_guard_skips_ticket_path(self, tmp_config):
        adapter = self._make_adapter(tmp_config)
        # No storm_guard wired — ticket helper returns None and end is a no-op.
        ticket = adapter._begin_dispatch_ticket(_intent())
        assert ticket is None
        await adapter._end_dispatch_ticket(None, _intent(), MagicMock(), 1)
        assert adapter.client.cancel_order.call_count == 0

    async def test_normal_dispatch_no_defensive_cancel(self, tmp_config):
        adapter = self._make_adapter(tmp_config)
        sg = StormGuard()
        adapter.set_storm_guard(sg)
        intent = _intent()
        trade = MagicMock(name="placed_trade")

        ticket = adapter._begin_dispatch_ticket(intent)
        assert ticket is not None
        # No HALT — end_dispatch returns False, no cancel issued.
        await adapter._end_dispatch_ticket(ticket, intent, trade, cmd_id=42)
        assert adapter.client.cancel_order.call_count == 0

    async def test_halt_during_dispatch_emits_defensive_cancel(self, tmp_config):
        """Begin ticket → HALT triggers → end_dispatch detects → cancel called."""
        adapter = self._make_adapter(tmp_config)
        sg = StormGuard()
        adapter.set_storm_guard(sg)
        intent = _intent()
        trade = MagicMock(name="placed_trade")

        ticket = adapter._begin_dispatch_ticket(intent)
        # Simulate HALT trigger between begin_dispatch and end_dispatch
        # (i.e. while broker.place_order is awaiting).
        sg.trigger_halt("simulated_mid_dispatch_halt")

        # Snapshot metric before / after
        metrics = MetricsRegistry.get()
        before = metrics.order_halt_post_dispatch_cancel_total._value.get()  # type: ignore[attr-defined]
        await adapter._end_dispatch_ticket(ticket, intent, trade, cmd_id=99)
        after = metrics.order_halt_post_dispatch_cancel_total._value.get()  # type: ignore[attr-defined]

        assert after - before == 1
        assert adapter.client.cancel_order.call_count == 1
        cancel_args, _ = adapter.client.cancel_order.call_args
        assert cancel_args[0] is trade

    async def test_halt_during_dispatch_no_trade_no_cancel(self, tmp_config):
        """Broker call failed (trade is None) → no cancel attempted."""
        adapter = self._make_adapter(tmp_config)
        sg = StormGuard()
        adapter.set_storm_guard(sg)
        intent = _intent()

        ticket = adapter._begin_dispatch_ticket(intent)
        sg.trigger_halt("halt_with_no_trade")
        await adapter._end_dispatch_ticket(ticket, intent, None, cmd_id=55)
        assert adapter.client.cancel_order.call_count == 0

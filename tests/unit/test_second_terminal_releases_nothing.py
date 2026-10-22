"""A second terminal status for an order that already ended must release nothing.

THESHOW, R47 (SIM), 2026-10-05: 34 orders (39 rows) got a ``FAILED`` 0.02-1.28 s
after their ``CANCELLED`` -- the retry of a cancel that had already landed was
rejected by the broker. ``R47.on_order`` decrements ``_pending_*`` by
``remaining_qty`` on every CANCELLED/FAILED, and Shioaji's order topic carries no
quantities, so ``remaining_qty`` was the full order quantity both times: the slot
was released twice, and the second release opens a slot that the strategy's NEW
quote in the meantime already occupies. (F1 fixed the same double release for an
order that FILLED; this is the other two ways an order ends twice.)

The adapter keeps a ledger of finished orders. The router asks it once per terminal
status event, BEFORE that event records the order as finished, so:

* the FIRST terminal of an order always keeps its ``remaining_qty`` -- including a
  live ``FAILED`` for insufficient margin, which is the only terminal that order
  will ever get and must free the slot;
* every later terminal of the same order carries ``remaining_qty == 0`` (status
  unchanged, so the strategy still clears its cancel-in-flight bookkeeping).

An order is finished when its terminal arrives, when a fill completes it, when the
300 s sweep gives up on it, or when its phantom record expires.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import OrderEvent, OrderStatus
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, RiskFeedback, Side
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter
from hft_platform.order.adapter import OrderAdapter

_SID = "R47_MAKER_TMF"
_TTL_S = 300.0


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _mock_infra():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _adapter(tmp_config: str) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=128), broker_client=client)
    adapter._live_orders_ttl_s = _TTL_S
    return adapter


def _intent(intent_id: int = 1, qty: int = 1, side: Side = Side.BUY) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_SID,
        symbol="TMFJ6",
        price=47000_0000,
        qty=qty,
        side=side,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )


def _register_live(adapter: OrderAdapter, intent: OrderIntent, *, age_s: float = 0.0) -> str:
    key = f"{intent.strategy_id}:{intent.intent_id}"
    adapter.live_orders[key] = {"id": f"broker-{intent.intent_id}"}
    adapter._live_orders_inserted_at[key] = time.monotonic() - age_s
    adapter._track_live_order_intent(key, intent, intent.side)
    return key


def _drain(sink: asyncio.Queue) -> list[RiskFeedback]:
    out: list[RiskFeedback] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


# --------------------------------------------------------------------------- #
# Adapter: the ledger of finished orders                                       #
# --------------------------------------------------------------------------- #


def test_an_order_that_never_ended_is_not_finished(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent())

    assert adapter.order_finished(key) is False


def test_note_terminal_answers_no_the_first_time_and_yes_afterwards(tmp_config):
    adapter = _adapter(tmp_config)

    assert adapter.note_terminal(f"{_SID}:1") is False
    assert adapter.note_terminal(f"{_SID}:1") is True
    assert adapter.note_terminal(f"{_SID}:2") is False, "another order is unaffected"


@pytest.mark.asyncio
async def test_a_terminal_status_finishes_the_order(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent())
    adapter.order_id_resolver.order_id_map["ORD1"] = key

    await adapter.on_terminal_state(_SID, "ORD1")

    assert adapter.order_finished(key) is True


@pytest.mark.asyncio
async def test_a_fill_that_completes_the_order_finishes_it(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(qty=1))

    assert await adapter.on_order_fill(key, 1) is True

    assert adapter.order_finished(key) is True


@pytest.mark.asyncio
async def test_a_partial_fill_does_not_finish_the_order(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(qty=2))

    await adapter.on_order_fill(key, 1)

    assert adapter.order_finished(key) is False


@pytest.mark.asyncio
async def test_the_ttl_sweep_finishes_the_orders_it_gives_up_on(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    swept = _register_live(adapter, _intent(intent_id=1), age_s=_TTL_S + 1)
    kept = _register_live(adapter, _intent(intent_id=2), age_s=1.0)
    adapter._live_orders_last_sweep_s = float("-inf")

    await adapter.sweep_stale_live_orders()

    assert adapter.order_finished(swept) is True
    assert adapter.order_finished(kept) is False


@pytest.mark.asyncio
async def test_a_sweep_release_that_never_reached_the_strategy_does_not_finish_the_order(tmp_config):
    """The order's real terminal is then the only release the strategy will get."""
    adapter = _adapter(tmp_config)
    full: asyncio.Queue = asyncio.Queue(maxsize=1)
    full.put_nowait(object())  # the sink is full: the feedback is dropped
    adapter.set_rejection_sink(full)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._live_orders_last_sweep_s = float("-inf")

    await adapter.sweep_stale_live_orders()

    assert adapter.order_finished(key) is False
    assert adapter.note_terminal(key) is False, "its real terminal keeps remaining_qty"


@pytest.mark.asyncio
async def test_a_sweep_without_a_rejection_sink_does_not_finish_the_order(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._live_orders_last_sweep_s = float("-inf")

    await adapter.sweep_stale_live_orders()

    assert adapter.order_finished(key) is False


@pytest.mark.asyncio
async def test_a_phantom_release_that_never_reached_the_strategy_does_not_finish_the_key(tmp_config):
    adapter = _adapter(tmp_config)
    full: asyncio.Queue = asyncio.Queue(maxsize=1)
    full.put_nowait(object())
    adapter.set_rejection_sink(full)
    with adapter._phantom_lock:
        phantom_key = adapter._register_phantom(_intent(intent_id=7))

    await adapter.release_stale_phantom_pendings(ttl_s=-1.0)

    assert adapter.order_finished(phantom_key) is False


@pytest.mark.asyncio
async def test_a_fully_filled_stale_order_is_finished_without_a_release(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._record_fill_ledger(key, 1)  # traded in full; the fill freed the slot
    adapter._live_orders_last_sweep_s = float("-inf")

    await adapter.sweep_stale_live_orders()

    assert adapter.order_finished(key) is True


@pytest.mark.asyncio
async def test_an_expired_phantom_record_finishes_its_order_key(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    intent = _intent(intent_id=7)
    with adapter._phantom_lock:
        phantom_key = adapter._register_phantom(intent)

    assert adapter.order_finished(phantom_key) is False
    await adapter.release_stale_phantom_pendings(ttl_s=-1.0)

    assert adapter.order_finished(phantom_key) is True


def test_the_finished_ledger_is_bounded(tmp_config):
    adapter = _adapter(tmp_config)
    adapter._finished_max = 3

    for i in range(10):
        adapter.note_terminal(f"{_SID}:{i}")

    assert len(adapter._finished_orders) == 3
    assert adapter.order_finished(f"{_SID}:9") is True
    assert adapter.order_finished(f"{_SID}:0") is False


def test_a_finished_entry_expires(tmp_config):
    adapter = _adapter(tmp_config)
    adapter._finished_ttl_s = -1.0  # every entry is already older than its TTL
    adapter.note_terminal(f"{_SID}:1")

    assert adapter.order_finished(f"{_SID}:1") is False
    assert adapter.note_terminal(f"{_SID}:1") is False, "an expired entry reads as a first terminal again"


# --------------------------------------------------------------------------- #
# Router: what a terminal status event reports                                 #
# --------------------------------------------------------------------------- #


def _order_event(*, status: OrderStatus, key: str = f"{_SID}:1", qty: int = 1) -> OrderEvent:
    return OrderEvent(
        order_id="ORD1",
        strategy_id=_SID,
        symbol="TMFJ6",
        status=status,
        submitted_qty=qty,
        filled_qty=0,
        remaining_qty=qty,  # what Shioaji's payload yields: nothing is ever filled
        price=47000_0000,
        side=Side.BUY,
        ingest_ts_ns=0,
        broker_ts_ns=0,
        client_order_id=key,
    )


def _router(tracker: object | None = None) -> ExecutionRouter:
    router = ExecutionRouter(
        bus=MagicMock(),
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=MagicMock(),
        terminal_handler=MagicMock(),
    )
    router.metrics = MagicMock()
    if tracker is not None:
        router.set_fill_tracker(tracker)
    return router


def test_second_terminal_carries_zero_remaining(tmp_config):
    router = _router(_adapter(tmp_config))
    cancelled = _order_event(status=OrderStatus.CANCELLED)
    failed = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(cancelled)
    router._release_slot_once(failed)

    assert cancelled.remaining_qty == 1, "the first terminal releases the slot"
    assert failed.remaining_qty == 0, "the second one must not release it again"
    assert failed.status == OrderStatus.FAILED, "status is untouched: the strategy still clears its bookkeeping"


def test_first_failed_terminal_still_releases(tmp_config):
    """An unfunded live account: the broker refuses the order. That FAILED is the
    only terminal the order will ever get, and the slot it held must be freed."""
    router = _router(_adapter(tmp_config))
    refused = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(refused)

    assert refused.remaining_qty == 1


@pytest.mark.asyncio
async def test_a_terminal_after_a_completing_fill_reports_nothing_remaining(tmp_config):
    adapter = _adapter(tmp_config)
    router = _router(adapter)
    key = _register_live(adapter, _intent())
    await adapter.on_order_fill(key, 1)
    late = _order_event(status=OrderStatus.CANCELLED, key=key)

    router._release_slot_once(late)

    assert late.remaining_qty == 0


@pytest.mark.asyncio
async def test_a_terminal_after_the_ttl_sweep_released_the_slot_reports_nothing_remaining(tmp_config):
    adapter = _adapter(tmp_config)
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    router = _router(adapter)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._live_orders_last_sweep_s = float("-inf")
    await adapter.sweep_stale_live_orders()
    late = _order_event(status=OrderStatus.CANCELLED, key=key)

    router._release_slot_once(late)

    assert late.remaining_qty == 0


@pytest.mark.parametrize("status", [OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED])
def test_a_non_terminal_status_is_never_zeroed_or_recorded(tmp_config, status):
    adapter = _adapter(tmp_config)
    router = _router(adapter)
    first = _order_event(status=status)
    second = _order_event(status=status)

    router._release_slot_once(first)
    router._release_slot_once(second)

    assert second.remaining_qty == 1
    assert adapter.order_finished(f"{_SID}:1") is False


def test_a_repeat_terminal_is_counted(tmp_config):
    router = _router(_adapter(tmp_config))

    router._release_slot_once(_order_event(status=OrderStatus.CANCELLED))
    router._release_slot_once(_order_event(status=OrderStatus.FAILED))

    router.metrics.order_repeat_terminal_total.labels.assert_called_once_with(status="FAILED")
    router.metrics.order_repeat_terminal_total.labels.return_value.inc.assert_called_once_with()


def test_an_event_without_a_client_order_id_is_not_looked_up():
    tracker = MagicMock()
    router = _router(tracker)
    event = _order_event(status=OrderStatus.FAILED, key="")

    router._release_slot_once(event)

    tracker.note_terminal.assert_not_called()
    assert event.remaining_qty == 1


def test_an_unattributed_first_terminal_is_not_tracked_so_an_attributed_repeat_is_not_zeroed(tmp_config):
    """A miss, never a false zero: with no client_order_id the first terminal cannot
    be recorded, so a later attributed one still counts as the first."""
    router = _router(_adapter(tmp_config))
    unattributed = _order_event(status=OrderStatus.CANCELLED, key="")
    attributed = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(unattributed)
    router._release_slot_once(attributed)

    assert unattributed.remaining_qty == 1
    assert attributed.remaining_qty == 1


def test_an_event_passes_through_untouched_without_a_tracker():
    router = _router()
    event = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(event)

    assert event.remaining_qty == 1


def test_a_tracker_that_cannot_answer_leaves_the_event_alone():
    """A MagicMock answers every call with a truthy MagicMock; only a real True counts."""
    router = _router(MagicMock())
    event = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(event)

    assert event.remaining_qty == 1


def test_a_tracker_without_the_method_leaves_the_event_alone():
    class _OldTracker:
        def filled_qty_for(self, key: str) -> int:
            return 0

    router = _router(_OldTracker())
    event = _order_event(status=OrderStatus.FAILED)

    router._release_slot_once(event)

    assert event.remaining_qty == 1


# --------------------------------------------------------------------------- #
# End to end: cancel, then the rejected retry -> R47 pending                   #
# --------------------------------------------------------------------------- #


async def _run_router(router: ExecutionRouter, published: list, expected: int) -> None:
    router.running = True
    task = asyncio.create_task(router.run())
    for _ in range(1000):  # run() has no one-shot mode: poll until it has published what we queued
        if len(published) >= expected:
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError(f"router published {len(published)} of {expected} events")
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _order_topic(op_type: str, op_code: str, seqno: str) -> RawExecEvent:
    return RawExecEvent(
        topic="order",
        data={
            "operation": {"op_type": op_type, "op_code": op_code, "op_msg": ""},
            "order": {"ordno": "ORD1", "seqno": seqno, "action": "Buy", "price": 47000.0, "quantity": 1},
            "status": {"exchange_ts": time.time()},
            "contract": {"code": "TMFJ6"},
        },
        ingest_ts_ns=time.time_ns(),
    )


@pytest.mark.asyncio
async def test_r47_pending_is_released_once_when_a_cancel_retry_is_rejected_after_the_cancel(
    tmp_config, tmp_path, monkeypatch
):
    """The shape measured on THESHOW: CANCELLED, then 0.15 s later FAILED for the
    same order. R47 must release the slot once; a NEW quote that took the slot in
    between must keep it."""
    from hft_platform.execution.positions import PositionStore
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'TMFJ6'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))

    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(intent_id=1))
    published: list = []
    bus = MagicMock()
    bus.publish_nowait = published.append
    bus.publish_many_nowait = lambda events: published.extend(events)
    with patch.dict("os.environ", {"HFT_RUST_POSITIONS": "0"}):
        position_store = PositionStore()
    position_store.metrics = None
    queue: asyncio.Queue = asyncio.Queue()
    router = ExecutionRouter(
        bus=bus,
        raw_queue=queue,
        order_id_map={"ORD1": key},
        position_store=position_store,
        terminal_handler=adapter.on_terminal_state,
    )
    router.set_fill_tracker(adapter)
    await queue.put(_order_topic("Cancel", "00", "S1"))
    await queue.put(_order_topic("Cancel", "99", "S2"))  # the retry: "already cancelled"

    await _run_router(router, published, expected=2)

    orders = [e for e in published if isinstance(e, OrderEvent)]
    assert [e.status for e in orders] == [OrderStatus.CANCELLED, OrderStatus.FAILED]
    assert [e.remaining_qty for e in orders] == [1, 0]

    strat = R47MakerStrategy(strategy_id=_SID, max_pos=1)
    strat._pending_buy["TMFJ6"] = 1  # the live order occupies the slot
    strat.on_order(orders[0])
    assert strat._pending_buy["TMFJ6"] == 0, "the cancel frees the slot"
    strat._pending_buy["TMFJ6"] = 1  # a NEW quote now rests in it
    strat.on_order(orders[1])
    assert strat._pending_buy["TMFJ6"] == 1, "the rejected retry must not free the new order's slot"

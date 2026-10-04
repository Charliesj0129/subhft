"""A fill must finish the order it filled.

Shioaji's order topic carries no fill quantity: the 1.5.6 stub's
``EventOrderStatusDict`` has no ``status`` key and the futures order detail has
no ``deal_quantity`` (see ``test_shioaji_sdk_payload_boundary.py``). So

* ``_map_status`` can never answer FILLED, and
* ``remaining_qty = quantity - 0`` on every cancel/reject that follows a fill.

Measured on THESHOW, 2026-09-03..10-03 (R47, SIM): a deal never removed the
order from ``live_orders``, and R47's pending slot was then released a second
time by whichever came first:

* a cancel sent ~0.14 s after the fill, rejected by the broker and normalised to
  FAILED (368 of 370 filled-then-FAILED orders), or
* the 300 s TTL sweep (``live_order_ttl_releases_total`` +2,169 in 31 days,
  matching the fills with no terminal row day by day).

Both end in ``R47.on_order`` / ``on_risk_feedback`` decrementing ``_pending_*``
again, which can open a slot while a live order still occupies it.
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

_TTL_S = 300.0
_SID = "R47_MAKER_TMF"


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


def _arm_sweep(adapter: OrderAdapter) -> None:
    adapter._live_orders_last_sweep_s = 0.0


# --------------------------------------------------------------------------- #
# Adapter: the fill finishes the order                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_full_fill_removes_the_order_from_live_orders(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(qty=1))

    completed = await adapter.on_order_fill(key, 1)

    assert completed is True
    assert key not in adapter.live_orders
    assert key not in adapter._live_order_intents
    assert key not in adapter._live_orders_inserted_at


@pytest.mark.asyncio
async def test_a_partial_fill_keeps_the_order_until_the_quantity_is_traded(tmp_config):
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(qty=2))

    assert await adapter.on_order_fill(key, 1) is False
    assert key in adapter.live_orders, "1 of 2 filled: the order is still resting"
    assert adapter.filled_qty_for(key) == 1

    assert await adapter.on_order_fill(key, 1) is True
    assert key not in adapter.live_orders


@pytest.mark.asyncio
async def test_a_filled_order_is_not_released_a_second_time_by_the_ttl_sweep(tmp_config):
    """The 2,169-per-month double release: on_fill already freed the slot."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)

    await adapter.on_order_fill(key, 1)
    _arm_sweep(adapter)
    await adapter.sweep_stale_live_orders()

    assert _drain(sink) == [], "a fully filled order must not produce live_order_ttl_expired"


@pytest.mark.asyncio
async def test_the_sweep_skips_the_release_when_the_fill_was_seen_but_the_order_was_not_completed(tmp_config):
    """A fill that beat the order's registration leaves it in live_orders. The
    sweep is the catch-all: it must consult the ledger before releasing."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._record_fill_ledger(key, 1)  # the fill arrived; completion did not run
    _arm_sweep(adapter)

    evicted = await adapter.sweep_stale_live_orders()

    assert evicted == 1
    assert _drain(sink) == []
    adapter.metrics.live_order_swept_after_fill_total.inc.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_an_unfilled_stale_order_is_still_released_by_the_sweep(tmp_config):
    """Control: the 2026-08-10 protection (an order that never acks must free
    its slot) is untouched."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    released = _drain(sink)
    assert len(released) == 1
    assert released[0].reason_code == "live_order_ttl_expired"


@pytest.mark.asyncio
async def test_a_partly_filled_stale_order_is_still_released(tmp_config):
    """1 of 2 traded, nothing else heard: the unfilled lot still holds a slot."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    key = _register_live(adapter, _intent(qty=2), age_s=_TTL_S + 1)
    await adapter.on_order_fill(key, 1)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    assert len(_drain(sink)) == 1


@pytest.mark.asyncio
async def test_a_cancel_aimed_at_a_just_filled_order_is_a_local_no_op(tmp_config):
    """Bug #29's idempotent-cancel path exists for this race but a fill never
    reached it. 368 cancels went to the broker after the fill and came back as
    rejects."""
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent())

    await adapter.on_order_fill(key, 1)

    assert adapter._is_recently_terminal(key) is True
    assert adapter._recently_terminal_orders[key][1] == "filled"


@pytest.mark.asyncio
async def test_a_reject_that_trails_the_fill_is_ignored_even_with_a_new_order_in_flight(tmp_config):
    """Without the early return the late FAILED is parked as "terminal before
    registration" whenever the strategy has a new order mid-dispatch."""
    adapter = _adapter(tmp_config)
    key = _register_live(adapter, _intent(intent_id=1))
    adapter.order_id_resolver.order_id_map["ORD1"] = key
    await adapter.on_order_fill(key, 1)
    adapter._pending_order_keys.add(f"{_SID}:2")  # the next quote is being dispatched

    await adapter.on_terminal_state(_SID, "ORD1")

    assert len(adapter._deferred_terminals) == 0


def test_the_fill_ledger_is_bounded(tmp_config):
    adapter = _adapter(tmp_config)
    adapter._fill_ledger_max = 3

    for i in range(10):
        adapter._record_fill_ledger(f"{_SID}:{i}", 1)

    assert len(adapter._fill_ledger) == 3
    assert adapter.filled_qty_for(f"{_SID}:9") == 1
    assert adapter.filled_qty_for(f"{_SID}:0") == 0


def test_a_ledger_entry_expires(tmp_config):
    adapter = _adapter(tmp_config)
    adapter._fill_ledger_ttl_s = -1.0  # every entry is already older than its TTL
    adapter._record_fill_ledger(f"{_SID}:1", 1)

    assert adapter.filled_qty_for(f"{_SID}:1") == 0


# --------------------------------------------------------------------------- #
# Router: the reject that follows a fill reports the quantity that traded      #
# --------------------------------------------------------------------------- #


def _order_event(*, status: OrderStatus, filled: int = 0, key: str = f"{_SID}:1") -> OrderEvent:
    return OrderEvent(
        order_id="ORD1",
        strategy_id=_SID,
        symbol="TMFJ6",
        status=status,
        submitted_qty=1,
        filled_qty=filled,
        remaining_qty=max(0, 1 - filled),
        price=47000_0000,
        side=Side.BUY,
        ingest_ts_ns=0,
        broker_ts_ns=0,
        client_order_id=key,
    )


def _router() -> ExecutionRouter:
    return ExecutionRouter(
        bus=MagicMock(),
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=MagicMock(),
        terminal_handler=MagicMock(),
    )


def test_a_reject_after_a_fill_reports_nothing_remaining():
    router = _router()
    tracker = MagicMock()
    tracker.filled_qty_for.return_value = 1
    router.set_fill_tracker(tracker)
    event = _order_event(status=OrderStatus.FAILED)
    assert event.remaining_qty == 1  # what Shioaji's payload yields today

    router._apply_fill_ledger(event)

    assert event.filled_qty == 1
    assert event.remaining_qty == 0


def test_the_ledger_never_lowers_a_fill_quantity_the_broker_reported():
    router = _router()
    tracker = MagicMock()
    tracker.filled_qty_for.return_value = 0
    router.set_fill_tracker(tracker)
    event = _order_event(status=OrderStatus.CANCELLED, filled=1)

    router._apply_fill_ledger(event)

    assert event.filled_qty == 1
    assert event.remaining_qty == 0


def test_an_order_event_passes_through_untouched_without_a_tracker():
    router = _router()
    event = _order_event(status=OrderStatus.FAILED)

    router._apply_fill_ledger(event)

    assert event.remaining_qty == 1


def test_an_order_event_without_a_client_order_id_is_not_looked_up():
    router = _router()
    tracker = MagicMock()
    router.set_fill_tracker(tracker)
    event = _order_event(status=OrderStatus.FAILED, key="")

    router._apply_fill_ledger(event)

    tracker.filled_qty_for.assert_not_called()


# --------------------------------------------------------------------------- #
# End to end: deal -> router -> adapter -> a cancel reject -> R47 pending      #
# --------------------------------------------------------------------------- #


async def _run_router(router: ExecutionRouter, settle_s: float = 0.2) -> None:
    router.running = True
    task = asyncio.create_task(router.run())
    await asyncio.sleep(settle_s)  # let the loop drain the queue; run() has no one-shot mode
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _deal(seqno: str) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "code": "TMFJ6",
            "action": "Buy",
            "price": 47000.0,
            "quantity": 1,
            "seqno": seqno,
            "ordno": "ORD1",
            "account_id": "acc1",
            "ts": str(time.time()),
        },
        ingest_ts_ns=time.time_ns(),
    )


def _rejected_cancel() -> RawExecEvent:
    """What the broker sends back for a cancel of an order that already traded."""
    return RawExecEvent(
        topic="order",
        data={
            "operation": {"op_type": "Cancel", "op_code": "99", "op_msg": "order already filled"},
            "order": {"ordno": "ORD1", "seqno": "S1", "action": "Buy", "price": 47000.0, "quantity": 1},
            "status": {"exchange_ts": time.time()},
            "contract": {"code": "TMFJ6"},
        },
        ingest_ts_ns=time.time_ns(),
    )


@pytest.mark.asyncio
async def test_r47_pending_is_released_once_when_a_cancel_is_rejected_after_the_fill(tmp_config, tmp_path, monkeypatch):
    """The whole chain on the exact shape measured on THESHOW: fill, then a
    cancel 0.1 s later comes back op_code != "00". R47 must end with pending 0,
    and must not have let a second order slip past its max_pos guard."""
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
    await queue.put(_deal("D1"))
    await queue.put(_rejected_cancel())

    await _run_router(router)

    fills = [e for e in published if type(e).__name__ == "FillEvent"]
    orders = [e for e in published if isinstance(e, OrderEvent)]
    assert len(fills) == 1, "the deal must reach the strategy"
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.FAILED
    assert orders[0].remaining_qty == 0, "the order traded in full; nothing is left to release"

    strat = R47MakerStrategy(strategy_id=_SID, max_pos=1)
    strat._pending_buy["TMFJ6"] = 1  # the live order occupies the slot
    strat.on_fill(fills[0])
    assert strat._pending_buy["TMFJ6"] == 0
    strat._pending_buy["TMFJ6"] = 1  # a NEW order now rests in the slot the fill freed
    strat.on_order(orders[0])
    assert strat._pending_buy["TMFJ6"] == 1, "a reject for the filled order must not free the new order's slot"

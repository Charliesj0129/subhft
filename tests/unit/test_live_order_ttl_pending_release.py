"""A dispatched order that never acks must give its pending slot back.

Measured on THESHOW 2026-08-10: two orders were dispatched at 07:48:58Z and
both got broker ids. Neither terminal callback ever reached the strategy — the
SDK payload was dropped at the type boundary (see
``test_shioaji_sdk_payload_boundary.py``). At 07:54:30Z the adapter's own
janitor logged::

    live_orders_stale_sweep evicted=2 remaining=0 ttl_s=300

so the adapter forgot the orders, but nothing told the strategy. R47's
``_pending_buy``/``_pending_sell`` stayed at 1, ``can_buy``/``can_sell`` stayed
False, and the strategy quoted nothing for the next two days.

The existing recovery janitor, ``release_stale_phantom_pendings`` (Bug D,
2026-04-20), does not cover this: a phantom is a *dispatch failure* that may
have reached the broker. These two orders dispatched successfully, so they were
never phantoms — ``phantom_recovery_releases_total`` correctly stayed at zero
for two days while the strategy was frozen.

This is the highest-regression-risk change in the set, because three separate
incident defences sit on the decrement path (``r47_maker.py:663-715``):

* **F3** (76-order burst, 2026-04-15 RC-2) — a release must not also clear
  ``_last_bid``/``_last_ask``; re-arming the price gate and the pending gate
  together produces a reject→resend amplification loop.
* **Bug 9** (2026-04-16) — feedback with ``side=None`` must not decrement both
  counters, which would reset max_pos protection.
* **DEC2-001** — feedback with ``was_approved=True`` must not decrement at all.

Every test below either pins the new release or pins one of those three.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, RiskFeedback, Side
from hft_platform.order.adapter import OrderAdapter

_TTL_S = 300.0


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #


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
        metrics = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _adapter(tmp_config: str) -> OrderAdapter:
    client = MagicMock()
    client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=128), broker_client=client)
    adapter._live_orders_ttl_s = _TTL_S
    return adapter


def _intent(intent_id: int = 1, side: Side = Side.BUY, symbol: str = "TMFH6") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="R47_MAKER_TMF",
        symbol=symbol,
        price=3728_0000,
        qty=1,
        side=side,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )


def _register_live(adapter: OrderAdapter, intent: OrderIntent, *, age_s: float) -> str:
    """Put an order into the state a successful dispatch leaves behind.

    Mirrors ``adapter.py:2552-2558`` plus the post-ack overwrite at 2663: the
    sentinel becomes a Trade object and the key leaves ``_pending_order_keys``.
    """
    order_key = f"{intent.strategy_id}:{intent.intent_id}"
    adapter.live_orders[order_key] = {"id": f"broker-{intent.intent_id}"}
    adapter._live_orders_inserted_at[order_key] = time.monotonic() - age_s
    adapter._track_live_order_intent(order_key, intent, intent.side)
    return order_key


def _drain(sink: asyncio.Queue) -> list[RiskFeedback]:
    out: list[RiskFeedback] = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


def _arm_sweep(adapter: OrderAdapter) -> None:
    """Clear the 60s rate limit so a sweep actually runs."""
    adapter._live_orders_last_sweep_s = 0.0


# --------------------------------------------------------------------------- #
# The release itself                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dispatched_order_past_ttl_releases_the_strategy_pending_slot(tmp_config):
    """The 2026-08-10 regression: the sweep evicted the order and told nobody."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    _arm_sweep(adapter)

    evicted = await adapter.sweep_stale_live_orders()

    assert evicted == 1
    feedbacks = _drain(sink)
    assert len(feedbacks) == 1, "the evicted order must produce exactly one release"
    assert feedbacks[0].reason_code == "live_order_ttl_expired"
    assert feedbacks[0].strategy_id == "R47_MAKER_TMF"
    assert feedbacks[0].symbol == "TMFH6"


@pytest.mark.asyncio
async def test_the_release_is_not_approved_so_the_strategy_actually_decrements(tmp_config):
    """DEC2-001: ``was_approved=True`` makes R47.on_risk_feedback return early.
    A release that arrives approved is a release that does nothing — which is
    precisely the phantom-pending trap Bug D had to be added to escape."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    assert _drain(sink)[0].was_approved is False


@pytest.mark.asyncio
async def test_the_release_carries_a_side_so_bug_9_cannot_fire(tmp_config):
    """Bug 9 (2026-04-16): side=None makes R47 log and return without releasing
    anything. A sideless release is a silent no-op."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(side=Side.SELL), age_s=_TTL_S + 1)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    assert _drain(sink)[0].side == Side.SELL


@pytest.mark.asyncio
async def test_an_order_still_inside_its_ttl_is_not_released(tmp_config):
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(), age_s=_TTL_S - 30)
    _arm_sweep(adapter)

    assert await adapter.sweep_stale_live_orders() == 0
    assert _drain(sink) == []


@pytest.mark.asyncio
async def test_an_order_still_in_flight_is_not_released(tmp_config):
    """A key in ``_pending_order_keys`` has not come back from place_order yet.
    The existing sweep already skips it; the release must not overtake it."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    adapter._pending_order_keys.add(key)
    _arm_sweep(adapter)

    assert await adapter.sweep_stale_live_orders() == 0
    assert _drain(sink) == []


@pytest.mark.asyncio
async def test_repeated_sweeps_release_the_same_order_only_once(tmp_config):
    """Two releases for one order would decrement pending twice and hand the
    strategy a slot it does not have."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    _register_live(adapter, _intent(), age_s=_TTL_S + 1)

    _arm_sweep(adapter)
    await adapter.sweep_stale_live_orders()
    _arm_sweep(adapter)
    await adapter.sweep_stale_live_orders()

    assert len(_drain(sink)) == 1


@pytest.mark.asyncio
async def test_a_terminal_callback_before_ttl_leaves_nothing_to_release(tmp_config):
    """The healthy path: the broker answers, the adapter drops the order, and
    the sweep must find no orphan to release."""
    adapter = _adapter(tmp_config)
    sink: asyncio.Queue = asyncio.Queue(maxsize=64)
    adapter.set_rejection_sink(sink)
    key = _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    async with adapter._live_orders_lock:
        adapter.live_orders.pop(key, None)
        adapter._live_orders_inserted_at.pop(key, None)
        adapter._untrack_live_order_intent(key)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    assert _drain(sink) == []


@pytest.mark.asyncio
async def test_a_broker_reconnect_invalidation_does_not_leak_release_state(tmp_config):
    """``invalidate_live_orders`` clears every live order on reconnect. It must
    clear the release sidecar too, or the map grows without bound across a
    reconnect-storm day (2026-07-25 saw thousands)."""
    adapter = _adapter(tmp_config)
    _register_live(adapter, _intent(intent_id=1), age_s=0)
    _register_live(adapter, _intent(intent_id=2), age_s=0)

    await adapter.invalidate_live_orders(reason="test")

    assert adapter._live_order_intents == {}


@pytest.mark.asyncio
async def test_the_release_is_counted_so_a_silent_broker_is_visible(tmp_config):
    """Two days of frozen quoting produced no counter anywhere. A release is
    the platform admitting the broker never answered — it must be measurable."""
    adapter = _adapter(tmp_config)
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    _register_live(adapter, _intent(), age_s=_TTL_S + 1)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    adapter.metrics.live_order_ttl_releases_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_a_successful_dispatch_registers_the_intent_for_release(tmp_config):
    """The sidecar has to be populated by the real dispatch path, not only by
    the test helper — ``live_orders`` itself holds broker Trade objects, which
    carry no strategy_id, symbol or side."""
    adapter = _adapter(tmp_config)
    adapter.metadata = MagicMock()
    adapter.metadata.price_scale = MagicMock(return_value=10000)
    codec = MagicMock()
    codec.encode_side = MagicMock(return_value="Buy")
    codec.encode_tif = MagicMock(return_value="ROD")
    codec.encode_price_type = MagicMock(return_value="LMT")
    adapter._broker_codec = codec
    intent = _intent(intent_id=7)

    await adapter._dispatch_to_api(
        OrderCommand(cmd_id=7, intent=intent, deadline_ns=time.time_ns() + 1_000_000_000, storm_guard_state=0)
    )

    key = "R47_MAKER_TMF:7"
    assert key in adapter._live_order_intents
    tracked_intent, tracked_side = adapter._live_order_intents[key]
    assert tracked_intent.intent_id == 7
    assert tracked_side == Side.BUY


@pytest.mark.asyncio
async def test_the_sidecar_is_bounded_by_the_live_order_cap(tmp_config):
    """``live_orders`` is hard-capped at ``_live_orders_max_size``. A sidecar
    that is not evicted alongside it is an unbounded map on the order path."""
    adapter = _adapter(tmp_config)
    adapter.set_rejection_sink(asyncio.Queue(maxsize=512))
    adapter._live_orders_max_size = 3
    for i in range(6):
        _register_live(adapter, _intent(intent_id=i), age_s=0)
    _arm_sweep(adapter)

    await adapter.sweep_stale_live_orders()

    assert len(adapter._live_order_intents) <= 3


# --------------------------------------------------------------------------- #
# What the strategy does with it — the three incident defences                 #
# --------------------------------------------------------------------------- #


def _r47() -> Any:
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    return R47MakerStrategy(strategy_id="R47_MAKER_TMF", max_pos=1)


def _ttl_feedback(side: Side, symbol: str = "TMFH6") -> RiskFeedback:
    return RiskFeedback(
        intent_id=1,
        strategy_id="R47_MAKER_TMF",
        symbol=symbol,
        reason_code="live_order_ttl_expired",
        timestamp_ns=0,
        side=side,
        was_approved=False,
    )


@pytest.mark.unit
def test_r47_gives_back_exactly_one_slot_on_a_ttl_release() -> None:
    r47 = _r47()
    r47._pending_buy["TMFH6"] = 1

    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    assert r47._pending_buy["TMFH6"] == 0
    assert r47._pending_sell.get("TMFH6", 0) == 0


@pytest.mark.unit
def test_a_ttl_release_never_touches_the_other_side() -> None:
    """Bug 9's failure mode was decrementing both counters at once."""
    r47 = _r47()
    r47._pending_buy["TMFH6"] = 1
    r47._pending_sell["TMFH6"] = 1

    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    assert r47._pending_sell["TMFH6"] == 1


@pytest.mark.unit
def test_a_ttl_release_does_not_rearm_the_price_gate() -> None:
    """F3, the 76-order burst defence: releasing pending *and* clearing the
    last quoted price re-arms both gates in the same tick, which is what turned
    a rejection into an amplification loop on 2026-04-15."""
    r47 = _r47()
    r47._pending_buy["TMFH6"] = 1
    r47._last_bid["TMFH6"] = 3728_0000
    r47._last_ask["TMFH6"] = 3733_0000

    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    assert r47._last_bid["TMFH6"] == 3728_0000
    assert r47._last_ask["TMFH6"] == 3733_0000


def _terminal(
    status: Any,
    side: Side = Side.BUY,
    *,
    client_order_id: str = "R47_MAKER_TMF:1",
    remaining_qty: int = 1,
) -> Any:
    from hft_platform.contracts.execution import OrderEvent

    return OrderEvent(
        order_id="broker-1",
        strategy_id="R47_MAKER_TMF",
        symbol="TMFH6",
        status=status,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=remaining_qty,
        price=3728_0000,
        side=side,
        ingest_ts_ns=0,
        broker_ts_ns=0,
        client_order_id=client_order_id,
    )


@pytest.mark.unit
def test_a_late_terminal_callback_does_not_decrement_a_slot_the_ttl_already_released() -> None:
    """The double-decrement hazard, and why it is closable.

    A RiskFeedback carries ``strategy_id`` + ``intent_id``; an OrderEvent
    carries ``client_order_id``, which *is* ``"strategy_id:intent_id"``. So the
    strategy can tell that a terminal callback arriving after the 300s TTL
    belongs to an order it has already been given back, and decline to pay for
    it twice. Without this, two pending orders on one side would collapse to
    zero after a single genuine fill elsewhere."""
    from hft_platform.contracts.execution import OrderStatus

    r47 = _r47()
    r47._pending_buy["TMFH6"] = 2
    r47.on_risk_feedback(_ttl_feedback(Side.BUY))
    assert r47._pending_buy["TMFH6"] == 1

    r47.on_order(_terminal(OrderStatus.CANCELLED))

    assert r47._pending_buy["TMFH6"] == 1, "the TTL release already paid for this order"


@pytest.mark.unit
def test_an_ordinary_terminal_callback_still_releases_its_slot() -> None:
    """The dedup must key on the released order only. A cancel for a different
    order is the normal path and must still decrement."""
    from hft_platform.contracts.execution import OrderStatus

    r47 = _r47()
    r47._pending_buy["TMFH6"] = 2
    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    r47.on_order(_terminal(OrderStatus.CANCELLED, client_order_id="R47_MAKER_TMF:2"))

    assert r47._pending_buy["TMFH6"] == 0


@pytest.mark.unit
def test_a_terminal_callback_without_a_client_order_id_falls_back_to_decrementing() -> None:
    """``client_order_id`` is resolved best-effort by the normalizer and can be
    empty. An unattributable callback must keep the pre-existing behaviour
    rather than silently stop releasing — the floor at zero bounds the worst
    case."""
    from hft_platform.contracts.execution import OrderStatus

    r47 = _r47()
    r47._pending_buy["TMFH6"] = 1
    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    r47.on_order(_terminal(OrderStatus.CANCELLED, client_order_id=""))

    assert r47._pending_buy["TMFH6"] == 0


@pytest.mark.unit
def test_a_fill_for_a_ttl_released_order_still_updates_the_position() -> None:
    """The dedup covers the pending counter only. If the order was in fact
    live at the broker and fills after the TTL, the position must still move —
    dropping it would leave the strategy blind to real exposure."""
    from hft_platform.contracts.execution import FillEvent

    r47 = _r47()
    r47._pending_buy["TMFH6"] = 1
    r47.on_risk_feedback(_ttl_feedback(Side.BUY))

    r47.on_fill(
        FillEvent(
            fill_id="F1",
            account_id="acct",
            order_id="broker-1",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFH6",
            side=Side.BUY,
            price=3728_0000,
            qty=1,
            fee=0,
            tax=0,
            ingest_ts_ns=0,
            match_ts_ns=0,
        )
    )

    assert r47._local_pos["TMFH6"] == 1


@pytest.mark.unit
def test_the_released_key_set_is_bounded() -> None:
    """A per-order set on a long-lived strategy is an unbounded map unless it
    is capped."""
    r47 = _r47()
    for i in range(5000):
        r47.on_risk_feedback(
            RiskFeedback(
                intent_id=i,
                strategy_id="R47_MAKER_TMF",
                symbol="TMFH6",
                reason_code="live_order_ttl_expired",
                timestamp_ns=0,
                side=Side.BUY,
                was_approved=False,
            )
        )

    assert len(r47._ttl_released_keys) <= 2048

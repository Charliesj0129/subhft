"""A stale live order with broker ids is checked at the broker before its slot is freed.

THESHOW 2026-11-13, key R47_MAKER_TMF:36179: placed, broker ids back at +30 ms,
cancel accepted, no terminal ever arrived; the cancel watch re-sent it at +64 s
and gave up at +125 s; the TTL sweep at +303 s released the strategy slot without
asking the broker whether the order was still working (2026-09-03..10-03: four
such orders filled 5.6-27 min later, three took R47 to +2 against max_pos=1).

The sweep now holds a stale key that has broker ids when the client can list its
working orders, runs one broker listing per sweep, and:

* listed open with the same ids -> re-cancel, keep the slot, new TTL (cap 3);
* not listed                    -> release (verified gone, not "unresolved");
* listing failed                -> hold; at TTL + max hold release unverified.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order import adapter as adapter_module
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"
_KEY = f"{_STRATEGY}:36179"
_T0 = 10_000.0


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


class _Clock:
    """``time`` for the adapter module only: monotonic is driven by the test."""

    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.fixture()
def clock():
    c = _Clock(_T0)
    with patch.object(adapter_module, "time", c):
        yield c


class _ListingClient(MagicMock):
    """A broker client whose class defines ``list_open_orders`` (the capability check)."""

    def list_open_orders(self) -> list[Any]:
        self.list_calls += 1
        if isinstance(self.listing, BaseException):
            raise self.listing
        return list(self.listing)


def _trade(*, ordno: str = "0B7F2A", seqno: str = "06FE0D", oid: str = "c0ffee01") -> Any:
    return SimpleNamespace(
        contract=SimpleNamespace(code="TMFL6"),
        order=SimpleNamespace(
            id=oid, seqno=seqno, ordno=ordno, action="Buy", price=27_850.0, quantity=1, custom_field=None
        ),
        status=SimpleNamespace(id=oid, status="Submitted"),
    )


def _intent(intent_id: int = 36179, intent_type: IntentType = IntentType.NEW, **kw: Any) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol="TMFL6",
        price=27_850_0000,
        qty=1,
        side=Side.BUY,
        intent_type=intent_type,
        tif=TIF.LIMIT,
        **kw,
    )


def _adapter(tmp_config: str, listing: Any, trade: Any) -> OrderAdapter:
    client = _ListingClient()
    client.listing = listing
    client.list_calls = 0
    client.place_order = MagicMock(return_value=trade)
    client.cancel_order = MagicMock(return_value=SimpleNamespace(ok=True))
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=64), broker_client=client)
    adapter.metadata = MagicMock()
    adapter.metadata.price_scale = MagicMock(return_value=10000)
    codec = MagicMock()
    codec.encode_side = MagicMock(return_value="Buy")
    codec.encode_tif = MagicMock(return_value="ROD")
    codec.encode_price_type = MagicMock(return_value="LMT")
    adapter._broker_codec = codec
    adapter._add_to_dlq = AsyncMock()
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    return adapter


def _cmd(intent: OrderIntent) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent.intent_id,
        intent=intent,
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _feedback(adapter: OrderAdapter) -> list[Any]:
    sink = adapter._rejection_sink
    return [sink.get_nowait() for _ in range(sink.qsize())]


def _verify_outcomes(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.live_order_stale_verify_total.labels.call_args_list]


async def _sweep_at(adapter: OrderAdapter, clock: _Clock, t: float) -> int:
    """One supervisor tick at ``_T0 + t``; waits for the background work it started."""
    clock.now = _T0 + t
    adapter._live_orders_last_sweep_s = float("-inf")
    evicted = await adapter.sweep_stale_live_orders()
    for _ in range(3):
        pending = [*adapter._cancel_retry_tasks]
        task = adapter._phantom_lookup_task
        if task is not None and not task.done():
            pending.append(task)
        if not pending:
            break
        await asyncio.gather(*pending)
    return evicted


async def _replay_36179(adapter: OrderAdapter, clock: _Clock) -> None:
    """place; ids back with the Trade; cancel ok; retried +64 s; gave up +125 s; sweep +303 s."""
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True
    assert adapter.live_orders[_KEY] is adapter.client.place_order.return_value
    clock.now = _T0 + 1.0
    await adapter._dispatch_to_api(_cmd(_intent(36180, IntentType.CANCEL, target_order_id=_KEY)))
    assert adapter.client.cancel_order.call_count == 1
    with patch("hft_platform.order.adapter.logger") as log:
        await _sweep_at(adapter, clock, 64.0)
        await _sweep_at(adapter, clock, 125.0)
    unconfirmed = [c.kwargs["outcome"] for c in log.warning.call_args_list if c.args[0] == "cancel_unconfirmed"]
    assert unconfirmed == ["retried", "gave_up"]
    assert adapter.client.cancel_order.call_count == 2
    assert _feedback(adapter) == []


# --------------------------------------------------------------------------- #
# The 36179 replay                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_order_listed_open_is_held_and_recancelled_36179(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade()], trade)  # the broker's own copy of the same order
    await _replay_36179(adapter, clock)

    evicted = await _sweep_at(adapter, clock, 303.0)

    assert evicted == 0
    assert adapter.live_orders.get(_KEY) is trade, "the slot stays with the working order"
    assert _feedback(adapter) == [], "no release while the broker shows the order open"
    assert adapter.client.list_calls == 1
    assert adapter.client.cancel_order.call_count == 3, "re-cancelled with the handle the platform holds"
    assert adapter.client.cancel_order.call_args.args[0] is trade
    assert _verify_outcomes(adapter) == ["found_open"]
    assert adapter._live_orders_inserted_at[_KEY] == _T0 + 303.0, "a new TTL starts"
    adapter.metrics.live_order_ttl_releases_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_stale_order_lookup_timeout_is_held_until_max_hold_then_released_unverified_36179(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, TimeoutError("update_status timed out"), trade)
    await _replay_36179(adapter, clock)

    await _sweep_at(adapter, clock, 303.0)
    await _sweep_at(adapter, clock, 2099.0)

    assert adapter.live_orders.get(_KEY) is trade
    assert _feedback(adapter) == []
    assert adapter.client.cancel_order.call_count == 2, "nothing re-cancelled without a broker answer"
    adapter.metrics.live_order_ttl_releases_total.inc.assert_not_called()
    adapter.metrics.live_orders_held_for_verify.set.assert_called_with(1)

    with patch("hft_platform.order.adapter.logger") as log:
        await _sweep_at(adapter, clock, 2100.0)

    assert _KEY not in adapter.live_orders
    (released,) = _feedback(adapter)
    assert released.was_approved is False
    assert released.side == Side.BUY
    assert released.reason_code == "live_order_ttl_expired"
    assert adapter.order_finished(_KEY)
    assert [c.args[0] for c in log.error.call_args_list].count("live_order_released_unverified") == 1
    adapter.metrics.live_order_ttl_releases_total.inc.assert_called_once()
    assert _verify_outcomes(adapter) == ["lookup_failed", "lookup_failed", "lookup_failed", "released_unverified"]
    adapter.metrics.live_orders_held_for_verify.set.assert_called_with(0)


@pytest.mark.asyncio
async def test_stale_order_absent_at_broker_is_released_verified_gone_36179(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [], trade)
    await _replay_36179(adapter, clock)

    with patch("hft_platform.order.adapter.logger") as log:
        await _sweep_at(adapter, clock, 303.0)

    assert _KEY not in adapter.live_orders
    assert _KEY not in adapter._live_order_intents
    (released,) = _feedback(adapter)
    assert released.was_approved is False
    assert released.side == Side.BUY
    assert adapter.order_finished(_KEY)
    assert _verify_outcomes(adapter) == ["gone"]
    adapter.metrics.live_order_ttl_releases_total.inc.assert_not_called()
    assert any(c.args[0] == "live_order_ttl_expired_verified_gone" for c in log.warning.call_args_list)


# --------------------------------------------------------------------------- #
# One release, whichever way the order ends                                     #
# --------------------------------------------------------------------------- #


async def _held(tmp_config: str, clock: _Clock, listing: Any) -> tuple[OrderAdapter, Any]:
    trade = _trade()
    adapter = _adapter(tmp_config, listing, trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True
    await _sweep_at(adapter, clock, 303.0)
    assert _KEY in adapter._stale_verify
    return adapter, trade


@pytest.mark.asyncio
async def test_terminal_during_verification_releases_exactly_once(tmp_config, clock):
    adapter, _trade_obj = await _held(tmp_config, clock, TimeoutError("down"))

    await adapter.on_terminal_state(_STRATEGY, "0B7F2A")  # the CANCELLED ack, by ordno
    assert adapter.order_finished(_KEY), "a repeat terminal releases nothing either"
    adapter.client.listing = []  # the broker no longer lists it either
    await _sweep_at(adapter, clock, 400.0)
    await _sweep_at(adapter, clock, 2200.0)

    assert _KEY not in adapter.live_orders
    assert _KEY not in adapter._stale_verify
    assert _feedback(adapter) == [], "the terminal is the one release; the sweep adds none"
    assert _verify_outcomes(adapter).count("terminal_during_verify") == 1
    assert "gone" not in _verify_outcomes(adapter)
    adapter.metrics.live_order_ttl_releases_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_fill_during_verification_releases_exactly_once(tmp_config, clock):
    adapter, _trade_obj = await _held(tmp_config, clock, TimeoutError("down"))

    assert await adapter.on_order_fill(_KEY, 1) is True  # on_fill freed the slot
    adapter.client.listing = []
    await _sweep_at(adapter, clock, 400.0)
    await _sweep_at(adapter, clock, 2200.0)

    assert _KEY not in adapter.live_orders
    assert _KEY not in adapter._stale_verify
    assert _feedback(adapter) == []
    assert _verify_outcomes(adapter).count("terminal_during_verify") == 1
    assert "released_unverified" not in _verify_outcomes(adapter)


# --------------------------------------------------------------------------- #
# Matching                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_listed_trade_bound_to_another_key_is_not_a_match(tmp_config, clock):
    """Same ids on the listed order, but they map to another platform order (recycled
    ids): not a match to re-cancel, and not proof the order is gone either -- held."""
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade()], trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True
    adapter.order_id_map["06FE0D"] = f"{_STRATEGY}:40001"  # the day session reissued the seqno

    await _sweep_at(adapter, clock, 303.0)

    assert _verify_outcomes(adapter) == ["ambiguous"]
    assert adapter.live_orders.get(_KEY) is trade
    assert adapter.client.cancel_order.call_count == 0
    assert _feedback(adapter) == []


@pytest.mark.asyncio
async def test_listed_superset_with_an_extra_id_bound_elsewhere_is_held_ambiguous(tmp_config, clock):
    """The listed order carries every held id plus one more that maps to another platform
    order. It may well be this order: releasing it as gone could free a working order's slot."""
    trade = _trade()
    listed = _trade()
    listed.seq_no = "0EXTRA"  # an extra id on the listed copy
    adapter = _adapter(tmp_config, [listed], trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True
    adapter.order_id_map["0EXTRA"] = f"{_STRATEGY}:40001"

    await _sweep_at(adapter, clock, 303.0)

    assert _verify_outcomes(adapter) == ["ambiguous"]
    assert adapter.live_orders.get(_KEY) is trade
    assert _feedback(adapter) == []
    assert adapter.client.cancel_order.call_count == 0


@pytest.mark.asyncio
async def test_listed_trade_with_partial_id_overlap_is_not_a_match(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade(ordno="0C0000")], trade)  # same seqno, another ordno
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True

    await _sweep_at(adapter, clock, 303.0)

    assert _verify_outcomes(adapter) == ["gone"]
    assert adapter.client.cancel_order.call_count == 0


@pytest.mark.asyncio
async def test_two_listed_trades_matching_are_ambiguous_and_held(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade(), _trade()], trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True

    await _sweep_at(adapter, clock, 303.0)

    assert _verify_outcomes(adapter) == ["ambiguous"]
    assert adapter.live_orders.get(_KEY) is trade
    assert adapter.client.cancel_order.call_count == 0
    assert _feedback(adapter) == []


# --------------------------------------------------------------------------- #
# Reconnect, cap, scope                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconnect_invalidation_clears_the_verify_set(tmp_config, clock):
    adapter, _trade_obj = await _held(tmp_config, clock, TimeoutError("down"))

    await adapter.invalidate_live_orders(reason="reconnect")

    assert adapter._stale_verify == {}
    adapter.metrics.live_orders_held_for_verify.set.assert_called_with(0)
    adapter.client.listing = []
    await _sweep_at(adapter, clock, 2200.0)
    assert _feedback(adapter) == [], "nothing left to release after the reconnect cleared it"


@pytest.mark.asyncio
async def test_recancel_is_capped_at_three_then_held_as_lookup_failed(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade()], trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True

    t = 0.0
    for _ in range(5):
        t = adapter._live_orders_inserted_at[_KEY] - _T0 + 301.0
        await _sweep_at(adapter, clock, t)

    assert adapter.client.cancel_order.call_count == 3
    assert adapter._stale_verify[_KEY].recancels == 3
    assert adapter.live_orders.get(_KEY) is trade
    assert _feedback(adapter) == []

    last_reset = adapter._live_orders_inserted_at[_KEY] - _T0
    await _sweep_at(adapter, clock, last_reset + 300.0 + 1800.0)
    assert _KEY not in adapter.live_orders
    assert len(_feedback(adapter)) == 1
    assert _verify_outcomes(adapter)[-1] == "released_unverified"
    assert adapter.client.cancel_order.call_count == 3


@pytest.mark.asyncio
async def test_lookup_from_another_trigger_does_not_spend_a_fresh_ttls_recancel(tmp_config, clock):
    """After a re-cancel the key has a new TTL. A lookup started 60 s later by something
    else (another stale key, a phantom) must not re-cancel it again."""
    trade = _trade()
    adapter = _adapter(tmp_config, [_trade()], trade)
    assert await adapter._dispatch_to_api(_cmd(_intent())) is True

    await _sweep_at(adapter, clock, 303.0)
    assert adapter.client.cancel_order.call_count == 1
    clock.now = _T0 + 363.0
    await adapter._settle_expired_phantoms(300.0)  # a lookup started for another reason

    assert adapter.client.cancel_order.call_count == 1
    assert adapter._stale_verify[_KEY].recancels == 1
    assert adapter.live_orders.get(_KEY) is trade
    assert _verify_outcomes(adapter) == ["found_open"]


@pytest.mark.asyncio
async def test_overflow_eviction_of_a_held_key_counts_evicted(tmp_config, clock):
    adapter = _adapter(tmp_config, TimeoutError("down"), _trade())
    for i, ordno in enumerate(("0B0001", "0B0002")):
        adapter.client.place_order = MagicMock(return_value=_trade(ordno=ordno, seqno=f"S{i}", oid=f"id{i}"))
        assert await adapter._dispatch_to_api(_cmd(_intent(36179 + i))) is True
        clock.now += 1.0
    adapter._live_orders_max_size = 1

    await _sweep_at(adapter, clock, 400.0)

    outcomes = _verify_outcomes(adapter)
    assert outcomes.count("evicted") == 1
    assert "terminal_during_verify" not in outcomes
    assert len(adapter._stale_verify) == 1


@pytest.mark.asyncio
async def test_one_listing_per_sweep_regardless_of_stale_count(tmp_config, clock):
    adapter = _adapter(tmp_config, [], _trade())
    for i, ordno in enumerate(("0B0001", "0B0002", "0B0003")):
        adapter.client.place_order = MagicMock(return_value=_trade(ordno=ordno, seqno=f"S{i}", oid=f"id{i}"))
        assert await adapter._dispatch_to_api(_cmd(_intent(36179 + i))) is True

    await _sweep_at(adapter, clock, 303.0)

    assert adapter.client.list_calls == 1
    assert _verify_outcomes(adapter) == ["gone", "gone", "gone"]
    assert len(_feedback(adapter)) == 3


@pytest.mark.asyncio
async def test_client_without_lister_keeps_the_unconditional_ttl_release(tmp_config, clock):
    trade = _trade()
    adapter = _adapter(tmp_config, [], trade)
    adapter.client = MagicMock()  # answers to any attribute, defines no list_open_orders
    adapter.live_orders[_KEY] = trade
    adapter._live_orders_inserted_at[_KEY] = _T0
    adapter._track_live_order_intent(_KEY, _intent(), Side.BUY)

    evicted = await _sweep_at(adapter, clock, 303.0)

    assert evicted == 1
    assert len(_feedback(adapter)) == 1
    assert adapter._phantom_lookup_task is None
    adapter.metrics.live_order_ttl_releases_total.inc.assert_called_once()


def test_registry_creates_each_stale_verify_outcome_at_zero() -> None:
    from hft_platform.observability.metrics import STALE_VERIFY_OUTCOMES, MetricsRegistry

    MetricsRegistry._instance = None
    try:
        counter = MetricsRegistry.get().live_order_stale_verify_total
        samples = {
            s.labels["outcome"]: s.value
            for metric in counter.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }
    finally:
        MetricsRegistry._instance = None

    assert set(STALE_VERIFY_OUTCOMES) == {
        "found_open",
        "gone",
        "lookup_failed",
        "ambiguous",
        "terminal_during_verify",
        "released_unverified",
        "evicted",
    }
    assert {o: samples.get(o) for o in STALE_VERIFY_OUTCOMES} == dict.fromkeys(STALE_VERIFY_OUTCOMES, 0.0)

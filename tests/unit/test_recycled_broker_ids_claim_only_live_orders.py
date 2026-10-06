"""A broker id the broker recycles may only claim an order that is still live.

THESHOW, R47 (SIM), 2026-10-06 09:35: the ack of order ``R47_MAKER_TMF:10228``
(broker order ``14B6DE``) carried an id that an order registered at 22:31 the night
before -- ``R47_MAKER_TMF:6145``, CANCELLED 11 h earlier -- also carried. The router
bound ``14B6DE`` to ``6145``, ``hft.orders`` and the fill were written under the dead
key, R47 tracked ``6145``, its cancel hit "Cancel target not found" and the order
filled. 11 fills that morning sat on ended orders' keys (intent ids 3620..8041).

``ordno`` never repeated in 3,138 fills since 9/20; ``seqno`` / ``id`` do. So ``ordno``
names an order unconditionally and every other id only while its order is live. Any
id registered for an order that has ended is also repointed by the next ack that
names a live order.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.router import ExecutionRouter

_ENDED = "R47_MAKER_TMF:6145"
_LIVE = "R47_MAKER_TMF:10228"


def _resolver(order_id_map: dict[str, str], live: set[str]) -> OrderIdResolver:
    return OrderIdResolver(order_id_map, is_live=lambda key: key in live)


def _ack(ordno: str, seqno: str, **extra: str) -> RawExecEvent:
    order = {"action": "Sell", "ordno": ordno, "seqno": seqno, **extra}
    return RawExecEvent(
        topic="order",
        data={
            "state": "Submitted",
            "payload": {"contract": {"code": "TMFJ6"}, "order": order, "status": {"status": "Submitted"}},
        },
        ingest_ts_ns=1,
    )


def _normalizer(order_id_map: dict[str, str], live: set[str]) -> ExecutionNormalizer:
    norm = ExecutionNormalizer(order_id_map=order_id_map)
    norm.order_id_resolver.is_live = lambda key: key in live
    return norm


def _router(norm: ExecutionNormalizer) -> Any:
    return SimpleNamespace(normalizer=norm)


# --------------------------------------------------------------------------- #
# The resolver                                                                 #
# --------------------------------------------------------------------------- #


def test_a_recycled_id_does_not_claim_an_ended_order():
    resolver = _resolver({"S1": _ENDED}, live=set())

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"], strong=1) is None


def test_a_recycled_id_claims_an_order_that_is_still_live():
    resolver = _resolver({"S1": _LIVE}, live={_LIVE})

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"], strong=1) == _LIVE


def test_ordno_names_an_ended_order_unconditionally():
    """A trailing status or deal of an order that has already ended must still resolve."""
    resolver = _resolver({"14B6DE": _ENDED}, live=set())

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"], strong=1) == _ENDED


def test_without_a_strong_count_every_candidate_counts_as_before():
    resolver = _resolver({"S1": _ENDED}, live=set())

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"]) == _ENDED
    assert resolver.resolve_strategy_id_from_candidates(["14B6DE", "S1"]) == "R47_MAKER_TMF"


def test_a_resolver_without_a_liveness_check_trusts_the_map():
    resolver = OrderIdResolver({"S1": _ENDED})

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"], strong=1) == _ENDED


def test_a_failing_liveness_check_falls_back_to_trusting_the_map():
    def _boom(_key: str) -> bool:
        raise RuntimeError("check bug")

    resolver = OrderIdResolver({"S1": _ENDED}, is_live=_boom)

    assert resolver.resolve_order_key_from_candidates(["14B6DE", "S1"], strong=1) == _ENDED


def test_the_strategy_id_of_an_ended_order_is_not_borrowed_by_a_recycled_id():
    resolver = _resolver({"S1": _ENDED}, live=set())

    assert resolver.resolve_strategy_id_from_candidates(["14B6DE", "S1"], strong=1) == "UNKNOWN"


# --------------------------------------------------------------------------- #
# The order ack: the 10/6 09:35 case                                           #
# --------------------------------------------------------------------------- #


def test_an_ack_with_a_recycled_seqno_is_not_attributed_to_the_ended_order():
    norm = _normalizer({"S1": _ENDED}, live=set())
    raw = _ack("14B6DE", "S1")
    raw.data["_resolved_order_key"] = _LIVE  # what _on_exec injects from the pending-fill index

    event = norm.normalize_order(raw)

    assert event is not None
    assert event.client_order_id == _LIVE
    assert event.client_order_id != _ENDED


def test_an_ack_with_a_recycled_seqno_and_nothing_to_inject_stays_unattributed():
    """Unattributed is honest; a dead key sends the strategy to cancel an order that is gone."""
    norm = _normalizer({"S1": _ENDED}, live=set())

    event = norm.normalize_order(_ack("14B6DE", "S1"))

    assert event is not None
    assert event.client_order_id == ""


def test_an_ack_names_a_live_order_through_its_registered_seqno():
    """The designed path: place_order registered the seqno, the ack adds the ordno."""
    norm = _normalizer({"S1": _LIVE}, live={_LIVE})

    event = norm.normalize_order(_ack("14B6DE", "S1"))

    assert event is not None
    assert event.client_order_id == _LIVE


def test_a_status_after_the_order_ended_still_resolves_by_ordno():
    norm = _normalizer({"14B6DE": _ENDED}, live=set())

    event = norm.normalize_order(_ack("14B6DE", "S1"))

    assert event is not None
    assert event.client_order_id == _ENDED


def test_an_ack_with_a_recycled_seqno_does_not_borrow_the_ended_orders_strategy():
    norm = _normalizer({"S1": "OTHER_STRAT:6145"}, live=set())

    event = norm.normalize_order(_ack("14B6DE", "S1"))

    assert event is not None
    assert event.strategy_id == "UNKNOWN"


# --------------------------------------------------------------------------- #
# The router backfill                                                          #
# --------------------------------------------------------------------------- #


def test_the_backfill_does_not_bind_a_new_ordno_to_an_ended_order():
    norm = _normalizer({"S1": _ENDED}, live=set())

    ExecutionRouter._backfill_order_id_map(_router(norm), _ack("14B6DE", "S1"))

    assert "14B6DE" not in norm.order_id_map, "this is the 10/6 09:35 binding"
    assert norm.order_id_map["S1"] == _ENDED, "an unclaimed entry is left as it was"


def test_the_backfill_binds_the_ordno_and_repoints_stale_ids_to_the_live_order():
    norm = _normalizer({"U1": _LIVE, "S1": _ENDED}, live={_LIVE})

    ExecutionRouter._backfill_order_id_map(_router(norm), _ack("14B6DE", "S1", id="U1"))

    assert norm.order_id_map["14B6DE"] == _LIVE
    assert norm.order_id_map["S1"] == _LIVE, "S1 was bound to an order that ended"
    assert norm.order_id_map["U1"] == _LIVE


def test_the_backfill_leaves_an_id_bound_to_another_live_order_alone():
    other = "R47_MAKER_TMF:10230"
    norm = _normalizer({"U1": _LIVE, "S1": other}, live={_LIVE, other})

    ExecutionRouter._backfill_order_id_map(_router(norm), _ack("14B6DE", "S1", id="U1"))

    assert norm.order_id_map["14B6DE"] == _LIVE
    assert norm.order_id_map["S1"] == other


def test_the_backfill_keeps_binding_through_ordno_for_an_ended_order():
    """A trailing ack of an order that already ended carries ids to register for its deals."""
    norm = _normalizer({"14B6DE": _ENDED}, live=set())

    ExecutionRouter._backfill_order_id_map(_router(norm), _ack("14B6DE", "S9"))

    assert norm.order_id_map["S9"] == _ENDED


def test_the_backfill_prefers_ordno_when_both_kinds_of_id_are_registered():
    norm = _normalizer({"14B6DE": _LIVE, "S1": _ENDED}, live={_LIVE})

    ExecutionRouter._backfill_order_id_map(_router(norm), _ack("14B6DE", "S1"))

    assert norm.order_id_map["S1"] == _LIVE


# --------------------------------------------------------------------------- #
# The broker-thread gate (_on_exec)                                            #
# --------------------------------------------------------------------------- #


def test_on_exec_does_not_borrow_the_strategy_of_an_ended_order_for_a_recycled_seqno(tmp_path):
    import asyncio
    import collections
    from unittest.mock import MagicMock, patch

    from hft_platform.contracts.strategy import Side
    from hft_platform.order import adapter as adapter_module
    from hft_platform.order.adapter import OrderAdapter
    from hft_platform.services.system import HFTSystem

    cfg = tmp_path / "order.yaml"
    cfg.write_text("rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n")
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder"),
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq"),
    ):
        mm.get.return_value = MagicMock()
        client = MagicMock()
        client.mode = "simulation"
        adapter = OrderAdapter(config_path=str(cfg), order_queue=asyncio.Queue(maxsize=8), broker_client=client)
    live_key = "R47_MAKER_TMF:10228"
    adapter.live_orders[live_key] = adapter_module._PENDING_SENTINEL
    asyncio.run(adapter._register_pending_fill(live_key, "TMFJ6", Side.SELL, "001F2A"))
    adapter.order_id_map["S1"] = "OTHER_STRAT:6145"  # an order that ended; its seqno came round again

    system = HFTSystem.__new__(HFTSystem)
    system.order_adapter = adapter
    system.loop = None
    system.running = False
    system._exec_overflow_buf = collections.deque(maxlen=16)
    system._EXEC_OVERFLOW_MAX = 16
    system._exec_overflow_counter = 0
    system._exec_overflow_evicted = 0
    system.storm_guard = MagicMock()
    data = {
        "state": "Submitted",
        "payload": {
            "contract": {"code": "TMFJ6"},
            "order": {"action": "Sell", "ordno": "14B6DE", "seqno": "S1"},
            "status": {"status": "Submitted"},
        },
    }

    system._on_exec("order", data)

    assert data["_resolved_strategy_id"] == "R47_MAKER_TMF", "the ended order's strategy was borrowed"
    assert data["_resolved_order_key"] == live_key

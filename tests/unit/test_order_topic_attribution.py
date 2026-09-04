"""Order callbacks must be attributed, and must not steal the fill's entry.

Measured on THESHOW 2026-09-02: ``hft.orders`` held 414 rows at 100 %
``strategy_id='UNKNOWN'`` and 100 % empty ``client_order_id``, while fills were
9/9 correctly attributed with 0 orphaned. ``hft.orders`` therefore could not be
joined to ``hft.fills`` -- the join key was empty on the orders side, so no
per-order TCA, audit or post-incident reconstruction was possible.

Two independent causes, both pinned below:

1. ``_on_exec``'s whole attribution block was gated on ``topic == "deal"``.
   Order callbacks never entered it, so the symbol+action fallback that saves
   the deal path was never even reached on the order path.
2. An order ACK cannot resolve by broker id at all. The callback arrives
   ~4.3 ms BEFORE ``place_order()`` returns the Trade carrying those ids::

     13:55:19.204785  order callback, map_size=1  -> resolve fails -> UNKNOWN
     13:55:19.209064  _register_broker_ids writes ordno  (4.3 ms too late)

   The pending-fill index is registered BEFORE dispatch, so it is the one
   source that is already populated when the ACK lands.

The trap this must not fall into: ``resolve_strategy_from_deal`` POPS the
entry. Reusing it on the order topic would attribute the ACK and leave the
fill that follows with nothing to match -- trading a 100 % order-attribution
failure for a fill-attribution failure. The order path peeks instead.
"""

from __future__ import annotations

import asyncio
import collections
import types
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import Side
from hft_platform.order.adapter import OrderAdapter

_STRATEGY = "R47_MAKER_TMF"
_SYMBOL = "TMFI6"
_ORDER_KEY = f"{_STRATEGY}:1"


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
    return OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=128),
        broker_client=client,
    )


def _with_pending(adapter: OrderAdapter) -> OrderAdapter:
    asyncio.get_event_loop_policy()
    asyncio.run(adapter._register_pending_fill(_ORDER_KEY, _SYMBOL, Side.BUY, "000001"))
    return adapter


# --------------------------------------------------------------------------- #
# The peek                                                                     #
# --------------------------------------------------------------------------- #


def test_peek_resolves_the_strategy_from_the_pending_index(tmp_config):
    adapter = _with_pending(_adapter(tmp_config))

    assert adapter.peek_strategy_from_pending(_SYMBOL, "Buy") == _STRATEGY


def test_peek_does_not_consume_so_the_fill_can_still_resolve(tmp_config):
    """The whole point: the ACK peeks, the fill consumes."""
    adapter = _with_pending(_adapter(tmp_config))

    assert adapter.peek_strategy_from_pending(_SYMBOL, "Buy") == _STRATEGY
    assert adapter.peek_strategy_from_pending(_SYMBOL, "Buy") == _STRATEGY, "a peek must be repeatable"
    assert adapter.resolve_strategy_from_deal(_SYMBOL, "Buy") == _STRATEGY, (
        "the fill must still find its entry after the ACK peeked at it"
    )
    assert adapter.resolve_strategy_from_deal(_SYMBOL, "Buy") is None, "the fill consumes"


def test_peek_returns_the_full_order_key_for_client_order_id(tmp_config):
    adapter = _with_pending(_adapter(tmp_config))

    assert adapter.peek_order_key_from_pending_candidates([_SYMBOL], "Buy") == _ORDER_KEY
    assert adapter.resolve_strategy_from_deal(_SYMBOL, "Buy") == _STRATEGY, "still not consumed"


def test_peek_respects_the_side(tmp_config):
    adapter = _with_pending(_adapter(tmp_config))

    assert adapter.peek_strategy_from_pending(_SYMBOL, "Sell") is None


def test_peek_of_an_unknown_symbol_is_none(tmp_config):
    adapter = _with_pending(_adapter(tmp_config))

    assert adapter.peek_strategy_from_pending("TXFI6", "Buy") is None


def test_peek_expires_stale_entries_like_the_consuming_path(tmp_config):
    """A TTL-expired entry is stale for every reader, peekers included."""
    adapter = _with_pending(_adapter(tmp_config))
    adapter._pending_fill_ttl_s = 0.0

    assert adapter.peek_strategy_from_pending(_SYMBOL, "Buy") is None
    assert adapter.resolve_strategy_from_deal(_SYMBOL, "Buy") is None


def test_strict_mode_refuses_an_ambiguous_peek(tmp_config):
    """Same ambiguity rule as the consuming path -- they share one body."""
    adapter = _adapter(tmp_config)
    adapter._pending_fifo_strict = True
    asyncio.run(adapter._register_pending_fill(_ORDER_KEY, _SYMBOL, Side.BUY, "000001"))
    asyncio.run(adapter._register_pending_fill("OTHER:2", _SYMBOL, Side.BUY, "000002"))

    assert adapter.peek_strategy_from_pending(_SYMBOL, "Buy") is None


# --------------------------------------------------------------------------- #
# The _on_exec gate                                                            #
# --------------------------------------------------------------------------- #


def _system(adapter: OrderAdapter):
    from hft_platform.services.system import HFTSystem

    system = HFTSystem.__new__(HFTSystem)
    system.order_adapter = adapter
    system.loop = None
    system.running = False
    system._exec_overflow_buf = collections.deque(maxlen=4096)
    system._EXEC_OVERFLOW_MAX = 4096
    system._exec_overflow_counter = 0
    system._exec_overflow_evicted = 0
    system.storm_guard = MagicMock()
    return system


def _order_payload() -> dict:
    return {
        "state": "Submitted",
        "payload": {
            "code": _SYMBOL,
            "action": "Buy",
            "order": {"ordno": "B2", "seqno": "A1"},
        },
    }


def test_order_topic_resolves_strategy_and_order_key(tmp_config):
    """The 2026-09-02 regression: the order topic never entered the block."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )
    data = _order_payload()

    system._on_exec("order", data)

    assert data.get("_resolved_strategy_id") == _STRATEGY
    assert data.get("_resolved_order_key") == _ORDER_KEY


def test_order_topic_attribution_leaves_the_fill_resolvable(tmp_config):
    """An ACK followed by its fill: both must be attributed."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )

    system._on_exec("order", _order_payload())
    deal = {"payload": {"code": _SYMBOL, "action": "Buy"}}
    system._on_exec("deal", deal)

    assert deal.get("_resolved_strategy_id") == _STRATEGY, (
        "the ACK consumed the entry the fill needed -- order attribution was traded for fill attribution"
    )


# --------------------------------------------------------------------------- #
# The real order-topic payload shape                                           #
# --------------------------------------------------------------------------- #
# Measured on THESHOW 2026-09-04, immediately after the fix above shipped:
# hft.orders took 5 new rows and every one was still strategy_id='UNKNOWN'
# with an empty client_order_id. The gate fix was correct and still produced
# nothing, because the block reads `action` and the symbol off the payload
# ROOT -- which is the *deal* topic's shape.
#
# The order topic nests them, exactly as normalize_order reads them
# (execution/normalizer.py:257 `d.get("contract")`, :273 `order.get("action")`):
#
#   deal :  {"code": "TMFI6", "action": "Buy", ...}          <- flat
#   order:  {"contract": {"code": "TMFI6"},                  <- nested
#            "order":    {"action": "Buy", "ordno": ...},
#            "status":   {...}}
#
# So `_action` was None on every real order callback and both attribution
# branches were skipped in silence. The tests above passed only because they
# were written with the flat shape.


def _real_order_payload() -> dict:
    """The shape shioaji actually delivers on the order topic."""
    return {
        "state": "Submitted",
        "payload": {
            "contract": {"code": _SYMBOL},
            "order": {"action": "Buy", "ordno": "B2", "seqno": "A1"},
            "status": {"status": "Submitted"},
        },
    }


def test_order_topic_resolves_from_the_nested_broker_payload(tmp_config):
    """action under `order` and the symbol under `contract` must both resolve."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )
    data = _real_order_payload()

    system._on_exec("order", data)

    assert data.get("_resolved_strategy_id") == _STRATEGY, (
        "the order topic nests action under `order`; reading only the payload "
        "root leaves _action None and skips attribution entirely"
    )
    assert data.get("_resolved_order_key") == _ORDER_KEY, (
        "client_order_id stays empty and hft.orders cannot join hft.fills"
    )


def test_nested_order_payload_still_leaves_the_fill_resolvable(tmp_config):
    """The nested path must peek, not consume, exactly like the flat one."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )

    ack = _real_order_payload()
    system._on_exec("order", ack)
    deal = {"payload": {"code": _SYMBOL, "action": "Buy"}}
    system._on_exec("deal", deal)

    # Assert the ACK resolved too, or this passes trivially against code that
    # never enters the block at all -- which is how the flat-shape tests above
    # stayed green through a 100 % production attribution failure.
    assert ack.get("_resolved_strategy_id") == _STRATEGY
    assert deal.get("_resolved_strategy_id") == _STRATEGY


def test_flat_order_payload_still_resolves_after_the_nested_fallback(tmp_config):
    """The root read must stay the first choice, not be replaced by the nested one."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )
    data = _order_payload()

    system._on_exec("order", data)

    assert data.get("_resolved_strategy_id") == _STRATEGY
    assert data.get("_resolved_order_key") == _ORDER_KEY


def test_nested_sell_payload_does_not_match_a_buy_pending(tmp_config):
    """Side must survive the nested read -- a SELL ACK must not take the BUY slot."""
    adapter = _with_pending(_adapter(tmp_config))
    system = _system(adapter)
    adapter.order_id_resolver = types.SimpleNamespace(
        resolve_strategy_id_from_candidates=lambda _c: "UNKNOWN",
    )
    sell = _real_order_payload()
    sell["payload"]["order"]["action"] = "Sell"
    buy = _real_order_payload()

    system._on_exec("order", sell)
    system._on_exec("order", buy)

    assert sell.get("_resolved_strategy_id") is None, "a SELL ACK took the BUY pending slot"
    assert sell.get("_resolved_order_key") is None
    # The BUY must still resolve, or the assertions above pass merely because
    # the nested read is broken rather than because the side is respected.
    assert buy.get("_resolved_strategy_id") == _STRATEGY

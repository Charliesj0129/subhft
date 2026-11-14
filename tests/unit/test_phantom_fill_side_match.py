"""Regression: a phantom fill must not be claimed by a phantom of the OPPOSITE side.

THESHOW 2026-11-11 (R47, TMFK6): the pending-fill index held no ``TMFK6:SELL``
entry, so ``resolve_phantom_fill`` fell through to the phantom-record fallback.
That loop compared only the symbol, so a SELL fill popped an older BUY phantom
of the same symbol (FIFO), credited the fill to the BUY key and returned. The
BUY phantom was then gone, so ``_release_phantoms`` could never hand its pending
slot back and the strategy's BUY slot leaked for good, while the real SELL
phantom stayed registered for a fill that had already been consumed.

The expired-ledger tier of the same method already matches side; the record loop
must too. Legacy records materialised from ``_phantom_order_keys`` carry a
synthesised ``side=BUY, qty=0`` intent that says nothing about the real side,
so they must stay matchable by either side.

A phantom carries the side that was actually placed at the broker, not just
``intent.side``: a FORCE_FLAT places on the closing side derived from the
position, so a failed FORCE_FLAT is registered with the tracked ``close_side``
(or side -1, "unknown, matches either", when it failed before it was tracked).
A reused ``intent_id`` can hold occurrences of both sides under one key; the
fill must claim the oldest occurrence of ITS side, and must not be hidden from
a matching occurrence behind an opposite-side head.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.order.adapter import _PENDING_SENTINEL, OrderAdapter

_STRATEGY = "R47_MAKER_TMF"
_SYMBOL = "TMFK6"
_BUY_ID = 1001  # older BUY phantom
_SELL_ID = 1002  # newer SELL phantom
_BUY_KEY = f"{_STRATEGY}:{_BUY_ID}"
_SELL_KEY = f"{_STRATEGY}:{_SELL_ID}"
_PRICE_X10000 = 32_150 * 10_000  # 32,150 index points, scaled int x10000


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text("rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n")
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
    client.mode = "simulation"
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=16),
        broker_client=client,
    )
    adapter._add_to_dlq = AsyncMock()
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=16))
    return adapter


def _intent(
    intent_id: int,
    side: Side,
    symbol: str = _SYMBOL,
    qty: int = 1,
    strategy_id: str = _STRATEGY,
    intent_type: IntentType = IntentType.NEW,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        price=_PRICE_X10000,
        qty=qty,
        side=side,
        intent_type=intent_type,
        tif=TIF.LIMIT,
    )


def _register(adapter: OrderAdapter, intent: OrderIntent) -> str:
    with adapter._phantom_lock:
        return adapter._register_phantom(intent)


_FILL_SEQ = 0


def _fill(side: Side, symbol: str = _SYMBOL, qty: int = 1) -> FillEvent:
    global _FILL_SEQ
    _FILL_SEQ += 1
    return FillEvent(
        fill_id=f"F{_FILL_SEQ:06d}",
        account_id="acc1",
        order_id=f"ORD{_FILL_SEQ:06d}",
        strategy_id="UNKNOWN",
        symbol=symbol,
        side=side,
        qty=qty,
        price=_PRICE_X10000,
        fee=0,
        tax=0,
        ingest_ts_ns=time.time_ns(),
        match_ts_ns=time.time_ns(),
    )


def _claimed(adapter: OrderAdapter, key: str) -> list[int]:
    """Sides of ``key``'s occurrences that hold a fill claim (2026-11-13: a claim keeps
    the record until the phantom's Trade confirms it, and notes no ledger before that)."""
    return [r.side for r in adapter._get_phantom_records().get(key, []) if r.claim is not None]


def _unclaimed_keys(adapter: OrderAdapter) -> frozenset[str]:
    return frozenset(k for k, recs in adapter._get_phantom_records().items() if any(r.claim is None for r in recs))


def _feedback(adapter: OrderAdapter) -> list[Any]:
    sink = adapter._rejection_sink
    return [sink.get_nowait() for _ in range(sink.qsize())]


def _buy_then_sell_phantoms(adapter: OrderAdapter) -> None:
    """Older BUY phantom registered first, newer SELL phantom second; empty pending-fill index."""
    assert _register(adapter, _intent(_BUY_ID, Side.BUY)) == _BUY_KEY
    assert _register(adapter, _intent(_SELL_ID, Side.SELL)) == _SELL_KEY
    assert not adapter._pending_fill_index, "the scenario needs the phantom-record fallback, not the index"


# --------------------------------------------------------------------------- #
# The fallback loop must match side                                            #
# --------------------------------------------------------------------------- #


def test_resolve_phantom_fill_skips_opposite_side_phantom_when_index_empty(tmp_config):
    adapter = _adapter(tmp_config)
    _buy_then_sell_phantoms(adapter)

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _STRATEGY

    records = adapter._get_phantom_records()
    assert _claimed(adapter, _SELL_KEY) == [int(Side.SELL)], "the SELL fill claims the SELL phantom"
    assert [r.intent.intent_id for r in records[_BUY_KEY]] == [_BUY_ID], "the BUY phantom is left alone"
    assert _claimed(adapter, _BUY_KEY) == []
    assert _unclaimed_keys(adapter) == frozenset({_BUY_KEY})
    assert adapter.filled_qty_for(_SELL_KEY) == 0, "the ledger is noted only when the Trade confirms the claim"
    assert adapter.filled_qty_for(_BUY_KEY) == 0, "the BUY order has not traded"


def test_resolve_phantom_fill_ignores_only_opposite_side_phantom(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(_BUY_ID, Side.BUY))

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) is None, "a BUY phantom cannot own a SELL fill"

    assert [r.intent.intent_id for r in adapter._get_phantom_records()[_BUY_KEY]] == [_BUY_ID]
    assert adapter.get_phantom_candidates() == frozenset({_BUY_KEY})
    assert adapter.filled_qty_for(_BUY_KEY) == 0
    assert len(adapter._get_expired_phantoms()) == 0


def test_resolve_phantom_fill_same_side_phantom_still_matches_oldest_first(tmp_config):
    """Guard against over-fixing: the side filter must not stop matching phantoms."""
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(_BUY_ID, Side.BUY))
    _register(adapter, _intent(_BUY_ID + 1, Side.BUY))
    _register(adapter, _intent(_SELL_ID, Side.SELL))

    assert adapter.resolve_phantom_fill(_fill(Side.BUY)) == _STRATEGY

    assert _unclaimed_keys(adapter) == frozenset({f"{_STRATEGY}:{_BUY_ID + 1}", _SELL_KEY})
    assert _claimed(adapter, _BUY_KEY) == [int(Side.BUY)], "FIFO: the oldest BUY phantom holds the claim"
    assert _claimed(adapter, _SELL_KEY) == []


def test_resolve_phantom_fill_still_requires_matching_symbol(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(_SELL_ID, Side.SELL, symbol="TMFJ6"))

    assert adapter.resolve_phantom_fill(_fill(Side.SELL, symbol=_SYMBOL)) is None

    assert adapter.get_phantom_candidates() == frozenset({_SELL_KEY})
    assert adapter.filled_qty_for(_SELL_KEY) == 0


# --------------------------------------------------------------------------- #
# The consequence: the BUY slot must still be releasable                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_buy_phantom_releases_pending_after_unrelated_sell_fill(tmp_config):
    adapter = _adapter(tmp_config)
    _buy_then_sell_phantoms(adapter)
    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _STRATEGY

    # The TTL runs out for whatever is still registered: only the BUY phantom is left.
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1

    (released,) = _feedback(adapter)
    assert released.intent_id == _BUY_ID, "the BUY slot is handed back, not the already-filled SELL"
    assert released.strategy_id == _STRATEGY
    assert released.symbol == _SYMBOL
    assert released.side == Side.BUY
    assert released.was_approved is False, "a release, not a phantom-pending marker"
    assert released.reason_code == "phantom_recovery_ttl_expired"
    assert adapter.get_phantom_candidates() == frozenset()
    assert adapter.order_finished(_BUY_KEY), "the released slot is on record so a later terminal releases 0"
    assert [(key, sym, side) for _ts, key, sym, side in adapter._get_expired_phantoms()] == [
        (_BUY_KEY, _SYMBOL, int(Side.BUY))
    ]


# --------------------------------------------------------------------------- #
# Legacy records carry no real side                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fill_side", [Side.SELL, Side.BUY], ids=["sell_fill", "buy_fill"])
def test_resolve_phantom_fill_legacy_zero_qty_record_still_matches_any_side(tmp_config, fill_side):
    adapter = _adapter(tmp_config)
    # Seeded only in the legacy view, as older tests and callers do: the adapter
    # materialises it as a synthetic intent with side=BUY, qty=0.
    legacy_key = f"{_STRATEGY}:legacy_{_BUY_ID}"
    adapter._phantom_order_keys[legacy_key] = (time.monotonic(), _SYMBOL)

    assert adapter.resolve_phantom_fill(_fill(fill_side)) == _STRATEGY

    assert _claimed(adapter, legacy_key) == [-1], "the legacy record holds the claim"
    assert _unclaimed_keys(adapter) == frozenset()


# --------------------------------------------------------------------------- #
# A phantom carries the side placed at the broker (FORCE_FLAT: the close side)  #
# --------------------------------------------------------------------------- #

_FF_ID = 2001
_FF_KEY = f"{_STRATEGY}:{_FF_ID}"


async def _fail_force_flat_dispatch(adapter: OrderAdapter, intent: OrderIntent, tracked_side: Side | None) -> None:
    """Drive ``_handle_dispatch_exception`` for a FORCE_FLAT whose place_order raised.

    ``tracked_side`` is the ``close_side`` that ``_dispatch_force_flat`` tracks with the
    in-flight sentinel before it calls the broker; None = it failed before it was tracked.
    The pending-fill index is left empty so a later fill takes the phantom-record fallback.
    """
    key = f"{intent.strategy_id}:{intent.intent_id}"
    if tracked_side is not None:
        async with adapter._live_orders_lock:
            adapter.live_orders[key] = _PENDING_SENTINEL
            adapter._pending_order_keys.add(key)
            adapter._track_live_order_intent(key, intent, tracked_side)
    await adapter._handle_dispatch_exception(intent=intent, cmd_id=1)
    assert not adapter._pending_fill_index, "the scenario needs the phantom-record fallback, not the index"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fill_side", "expected"),
    [(Side.SELL, _STRATEGY), (Side.BUY, None)],
    ids=["close_side_fill_resolves", "intent_side_fill_does_not"],
)
async def test_failed_force_flat_phantom_matches_the_tracked_close_side_not_intent_side(
    tmp_config, fill_side, expected
):
    adapter = _adapter(tmp_config)
    # The intent says BUY, but the position being flattened is long: the order went out as a SELL.
    intent = _intent(_FF_ID, Side.BUY, intent_type=IntentType.FORCE_FLAT)
    await _fail_force_flat_dispatch(adapter, intent, tracked_side=Side.SELL)
    assert adapter.get_phantom_candidates() == frozenset({_FF_KEY})

    assert adapter.resolve_phantom_fill(_fill(fill_side)) == expected

    if expected is None:
        assert adapter.get_phantom_candidates() == frozenset({_FF_KEY}), "the phantom is left for its own fill"
        assert adapter.filled_qty_for(_FF_KEY) == 0
    else:
        assert _claimed(adapter, _FF_KEY) == [int(Side.SELL)], "the SELL fill claims the phantom"
        assert _unclaimed_keys(adapter) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_side", [Side.SELL, Side.BUY], ids=["sell_fill", "buy_fill"])
async def test_failed_force_flat_phantom_without_tracked_order_matches_either_side(tmp_config, fill_side):
    adapter = _adapter(tmp_config)
    # Failed before the live order was tracked: nothing says which side went out (side -1).
    intent = _intent(_FF_ID, Side.BUY, intent_type=IntentType.FORCE_FLAT)
    assert _FF_KEY not in adapter._live_order_intents
    await _fail_force_flat_dispatch(adapter, intent, tracked_side=None)
    assert adapter.get_phantom_candidates() == frozenset({_FF_KEY})

    assert adapter.resolve_phantom_fill(_fill(fill_side)) == _STRATEGY

    assert _claimed(adapter, _FF_KEY) == [-1], "the unknown-side phantom holds the claim"
    assert _unclaimed_keys(adapter) == frozenset()


@pytest.mark.asyncio
async def test_expired_force_flat_phantom_ledger_records_the_tracked_close_side(tmp_config):
    adapter = _adapter(tmp_config)
    intent = _intent(_FF_ID, Side.BUY, intent_type=IntentType.FORCE_FLAT)
    await _fail_force_flat_dispatch(adapter, intent, tracked_side=Side.SELL)

    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1

    assert adapter.get_phantom_candidates() == frozenset()
    assert [(key, sym, side) for _ts, key, sym, side in adapter._get_expired_phantoms()] == [
        (_FF_KEY, _SYMBOL, int(Side.SELL))
    ], "the ledger holds the side placed at the broker (SELL), not intent.side (BUY)"
    # Behavioural consequence: a late SELL fill is still claimed, a BUY fill is not.
    assert adapter.resolve_phantom_fill(_fill(Side.BUY)) is None
    assert len(adapter._get_expired_phantoms()) == 1
    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _STRATEGY
    assert len(adapter._get_expired_phantoms()) == 0


# --------------------------------------------------------------------------- #
# A reused intent_id holds occurrences of both sides under one key              #
# --------------------------------------------------------------------------- #

_A_STRATEGY = "STRAT_A"
_B_STRATEGY = "STRAT_B"
_A_ID = 3001
_B_ID = 3002
_A_KEY = f"{_A_STRATEGY}:{_A_ID}"
_B_KEY = f"{_B_STRATEGY}:{_B_ID}"


def _sides(adapter: OrderAdapter, key: str) -> list[int]:
    return [int(r.intent.side) for r in adapter._get_phantom_records()[key]]


def test_resolve_phantom_fill_scans_past_opposite_side_head_of_a_reused_intent_id(tmp_config):
    adapter = _adapter(tmp_config)
    # Key A holds [BUY, SELL] (intent_id reused); key B, registered after both, holds [SELL].
    assert _register(adapter, _intent(_A_ID, Side.BUY, strategy_id=_A_STRATEGY)) == _A_KEY
    assert _register(adapter, _intent(_A_ID, Side.SELL, strategy_id=_A_STRATEGY)) == _A_KEY
    assert _register(adapter, _intent(_B_ID, Side.SELL, strategy_id=_B_STRATEGY)) == _B_KEY
    assert _sides(adapter, _A_KEY) == [int(Side.BUY), int(Side.SELL)]
    assert not adapter._pending_fill_index, "the scenario needs the phantom-record fallback, not the index"

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _A_STRATEGY, "the older SELL under A wins over B's"

    assert _sides(adapter, _A_KEY) == [int(Side.BUY), int(Side.SELL)], "the record is kept with the claim"
    assert _claimed(adapter, _A_KEY) == [int(Side.SELL)], "A's SELL occurrence holds the claim, not its BUY"
    assert _sides(adapter, _B_KEY) == [int(Side.SELL)], "B is untouched"
    assert _claimed(adapter, _B_KEY) == []


def test_resolve_phantom_fill_pending_index_pops_the_same_side_occurrence_of_a_reused_intent_id(tmp_config):
    adapter = _adapter(tmp_config)
    assert _register(adapter, _intent(_A_ID, Side.BUY, strategy_id=_A_STRATEGY)) == _A_KEY
    assert _register(adapter, _intent(_A_ID, Side.SELL, strategy_id=_A_STRATEGY)) == _A_KEY
    # The SELL submission registered its early-fill correlation; the BUY one did not (or already settled).
    adapter._pending_fill_index[f"{_SYMBOL}:SELL"] = [_A_KEY]

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _A_STRATEGY

    assert not adapter._pending_fill_index, "the SELL pending-fill entry is consumed"
    assert _claimed(adapter, _A_KEY) == [int(Side.SELL)], "the BUY occurrence is not the one claimed"
    assert _sides(adapter, _A_KEY) == [int(Side.BUY), int(Side.SELL)]
    assert adapter.get_phantom_candidates() == frozenset({_A_KEY})
    assert adapter.filled_qty_for(_A_KEY) == 0, "the ledger waits for the Trade's confirmation"


def test_resolve_phantom_fill_pending_index_keeps_all_phantoms_when_none_match_the_side(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(_A_ID, Side.BUY, strategy_id=_A_STRATEGY))
    adapter._pending_fill_index[f"{_SYMBOL}:SELL"] = [_A_KEY]

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _A_STRATEGY, "the index still attributes the fill"

    assert adapter.get_phantom_candidates() == frozenset({_A_KEY}), "a BUY phantom is never popped by a SELL fill"
    assert _sides(adapter, _A_KEY) == [int(Side.BUY)]


# --------------------------------------------------------------------------- #
# The fallback claims the oldest eligible occurrence across ALL keys            #
# --------------------------------------------------------------------------- #

_OLD_SYMBOL = "TMFJ6"


def _stamp_registration_order(adapter: OrderAdapter, *keys_in_order: str) -> None:
    """Give the registered occurrences strictly increasing ``monotonic_ts`` in the order listed.

    ``keys_in_order`` names a key once per occurrence, in registration order; the n-th mention
    of a key stamps that key's n-th occurrence. Registration reads the real clock, whose ticks can
    tie, so the ordering is set explicitly instead of slept for. All stamps are a few seconds in
    the past, well inside the 2 h freshness window.
    """
    base = time.monotonic() - 10.0
    seen: dict[str, int] = {}
    records = adapter._get_phantom_records()
    for n, key in enumerate(keys_in_order):
        i = seen.get(key, 0)
        seen[key] = i + 1
        records[key][i] = records[key][i]._replace(monotonic_ts=base + n)
    stamps = [r.monotonic_ts for recs in records.values() for r in recs]
    assert len(set(stamps)) == len(stamps), "the scenario needs strictly ordered registration times"


def test_resolve_phantom_fill_claims_oldest_occurrence_across_keys_not_first_key_in_dict_order(tmp_config):
    adapter = _adapter(tmp_config)
    # Registration order (oldest first):
    #   1. A:id BUY  on OLD   (key A is created first, so it iterates first)
    #   2. B:id2 SELL on NEW  (the oldest occurrence that can own a SELL fill on NEW)
    #   3. A:id SELL on NEW   (the intent_id is reused: newer, but it sits under the first key)
    assert _register(adapter, _intent(_A_ID, Side.BUY, symbol=_OLD_SYMBOL, strategy_id=_A_STRATEGY)) == _A_KEY
    assert _register(adapter, _intent(_B_ID, Side.SELL, strategy_id=_B_STRATEGY)) == _B_KEY
    assert _register(adapter, _intent(_A_ID, Side.SELL, strategy_id=_A_STRATEGY)) == _A_KEY
    _stamp_registration_order(adapter, _A_KEY, _B_KEY, _A_KEY)
    assert list(adapter._get_phantom_records()) == [_A_KEY, _B_KEY], "A iterates before B"
    assert not adapter._pending_fill_index, "the scenario needs the phantom-record fallback, not the index"

    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _B_STRATEGY, "B's SELL is older than A's reused one"

    assert _claimed(adapter, _B_KEY) == [int(Side.SELL)], "B's only occurrence holds the claim"
    assert [(r.symbol, r.side) for r in adapter._get_phantom_records()[_A_KEY]] == [
        (_OLD_SYMBOL, int(Side.BUY)),
        (_SYMBOL, int(Side.SELL)),
    ], "A keeps both occurrences, including its newer SELL on NEW"
    assert _claimed(adapter, _A_KEY) == []
    assert _unclaimed_keys(adapter) == frozenset({_A_KEY})
    # The next SELL fill on NEW then finds A's occurrence: nothing was lost, only ordered.
    assert adapter.resolve_phantom_fill(_fill(Side.SELL)) == _A_STRATEGY
    assert [(r.symbol, r.side, r.claim is not None) for r in adapter._get_phantom_records()[_A_KEY]] == [
        (_OLD_SYMBOL, int(Side.BUY), False),
        (_SYMBOL, int(Side.SELL), True),
    ]


# --------------------------------------------------------------------------- #
# A malformed side must not break registration                                  #
# --------------------------------------------------------------------------- #

_BAD_ID = 4001
_BAD_KEY = f"{_STRATEGY}:{_BAD_ID}"


@pytest.mark.asyncio
async def test_failed_new_intent_with_missing_side_registers_unknown_side_phantom_without_raising(tmp_config):
    adapter = _adapter(tmp_config)
    # OrderIntent is an unvalidated slots dataclass, so side=None builds; cast(Any, ...) only
    # keeps the static type honest. Registration runs after the phantom-pending feedback went
    # out, so a raise here would drop the rest of the worker's batch.
    intent = _intent(_BAD_ID, cast(Any, None))
    assert intent.side is None

    await adapter._handle_dispatch_exception(intent=intent, cmd_id=1)  # must not raise

    (record,) = adapter._get_phantom_records()[_BAD_KEY]
    assert record.side == -1, "unknown side, not a crash and not a guessed BUY"
    assert record.symbol == _SYMBOL
    assert record.intent is intent
    assert adapter.get_phantom_candidates() == frozenset({_BAD_KEY})
    # Unknown side matches either side: the order may have gone out as either.
    assert adapter.resolve_phantom_fill(_fill(Side.BUY)) == _STRATEGY
    assert _claimed(adapter, _BAD_KEY) == [-1]
    assert _unclaimed_keys(adapter) == frozenset()

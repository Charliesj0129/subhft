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
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter

_STRATEGY = "R47_MAKER_TMF"
_SYMBOL = "TMFK6"
_BUY_ID = 28866  # older BUY phantom
_SELL_ID = 28964  # newer SELL phantom
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


def _intent(intent_id: int, side: Side, symbol: str = _SYMBOL, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol=symbol,
        price=_PRICE_X10000,
        qty=qty,
        side=side,
        intent_type=IntentType.NEW,
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
    assert _SELL_KEY not in records, "the SELL fill consumes the SELL phantom"
    assert [r.intent.intent_id for r in records[_BUY_KEY]] == [_BUY_ID], "the BUY phantom is left alone"
    assert adapter.get_phantom_candidates() == frozenset({_BUY_KEY})
    assert adapter.filled_qty_for(_SELL_KEY) == 1, "the fill is credited to the SELL order"
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

    assert adapter.get_phantom_candidates() == frozenset({f"{_STRATEGY}:{_BUY_ID + 1}", _SELL_KEY})
    assert adapter.filled_qty_for(_BUY_KEY) == 1, "FIFO: the oldest BUY phantom is credited"
    assert adapter.filled_qty_for(_SELL_KEY) == 0


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

    assert adapter.get_phantom_candidates() == frozenset(), "the legacy record is consumed"
    assert legacy_key not in adapter._phantom_order_keys
    assert adapter.filled_qty_for(legacy_key) == 1

"""Residuals after #561 on THESHOW (R47, SIM, 2026-10-08).

* A timed-out ``place_order`` whose Trade never came back lived on at the broker; its
  300 s TTL ran out at 03:20Z and it filled at 04:36Z as strategy UNKNOWN (1 of 217).
  The expired phantom is now remembered, so that fill is claimed by its strategy.
* The strategy cancelled an order by the broker's ack id 20 ms before the order's Trade
  was bound; the cancel went to the DLQ as "target not found" although #561 cancels the
  bound order anyway. It is now recognised as covered.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order import adapter as adapter_module
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"


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
    intent_id: int, side: Side = Side.SELL, symbol: str = "TMFJ6", intent_type: IntentType = IntentType.NEW, **kw: Any
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol=symbol,
        price=49_5000_0000 // 10000 * 10000,
        qty=1,
        side=side,
        intent_type=intent_type,
        tif=TIF.LIMIT,
        **kw,
    )


def _register(adapter: OrderAdapter, intent: OrderIntent) -> str:
    with adapter._phantom_lock:
        return adapter._register_phantom(intent)


def _fill(side: Side = Side.SELL, symbol: str = "TMFJ6") -> Any:
    return MagicMock(symbol=symbol, side=side, qty=1)


def _outcomes(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.phantom_bound_total.labels.call_args_list]


# --------------------------------------------------------------------------- #
# A fill that outlives the phantom TTL                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_fill_after_the_phantom_ttl_is_claimed_by_its_strategy(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(3767))
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1
    assert adapter.get_phantom_candidates() == frozenset()

    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY
    assert _outcomes(adapter) == ["fill_claimed_after_expiry"]


@pytest.mark.asyncio
async def test_an_expired_phantom_claims_only_one_fill(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(3767))
    await adapter.release_stale_phantom_pendings(ttl_s=0.0)

    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY
    assert adapter.resolve_phantom_fill(_fill()) is None


@pytest.mark.asyncio
async def test_an_expired_phantom_does_not_claim_the_other_side_or_symbol(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(3767, side=Side.SELL))
    await adapter.release_stale_phantom_pendings(ttl_s=0.0)

    assert adapter.resolve_phantom_fill(_fill(side=Side.BUY)) is None
    assert adapter.resolve_phantom_fill(_fill(symbol="TMFK6")) is None
    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY, "the matching fill still claims it"


@pytest.mark.asyncio
async def test_an_expired_phantom_is_forgotten_after_the_retention_window(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(3767))
    await adapter.release_stale_phantom_pendings(ttl_s=0.0)
    expired_at, *rest = adapter._get_expired_phantoms().pop()
    adapter._get_expired_phantoms().append((expired_at - adapter_module._EXPIRED_PHANTOM_RETENTION_S - 1, *rest))

    assert adapter.resolve_phantom_fill(_fill()) is None
    assert len(adapter._get_expired_phantoms()) == 0


@pytest.mark.asyncio
async def test_a_live_phantom_is_claimed_before_an_expired_one(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(1))
    await adapter.release_stale_phantom_pendings(ttl_s=0.0)
    live_key = _register(adapter, _intent(2))

    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY
    assert live_key not in adapter.get_phantom_candidates(), "the live record took the fill"
    assert len(adapter._get_expired_phantoms()) == 1, "the expired one stays for the next fill"


def test_the_expired_ledger_is_bounded(tmp_config):
    adapter = _adapter(tmp_config)
    for i in range(adapter_module._EXPIRED_PHANTOM_MAX + 10):
        adapter._get_expired_phantoms().append((0.0, f"{_STRATEGY}:{i}", "TMFJ6", 1))

    assert len(adapter._get_expired_phantoms()) == adapter_module._EXPIRED_PHANTOM_MAX


# --------------------------------------------------------------------------- #
# A cancel by broker id that beats the Trade                                   #
# --------------------------------------------------------------------------- #


def _cancel(target: str) -> OrderCommand:
    return OrderCommand(
        cmd_id=4175,
        intent=_intent(4175, intent_type=IntentType.CANCEL, target_order_id=target),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


@pytest.mark.asyncio
async def test_a_cancel_by_unknown_broker_id_while_a_phantom_awaits_its_trade_is_deferred(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(4173))

    ok = await adapter._dispatch_to_api(_cancel("158DFA"))

    assert ok is True
    adapter._add_to_dlq.assert_not_called()
    assert _outcomes(adapter) == ["cancel_target_deferred"]
    adapter.client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_a_cancel_by_unknown_broker_id_with_no_phantom_still_goes_to_the_dlq(tmp_config):
    adapter = _adapter(tmp_config)

    ok = await adapter._dispatch_to_api(_cancel("158DFA"))

    assert ok is False
    adapter._add_to_dlq.assert_awaited_once()
    assert "cancel_target_deferred" not in _outcomes(adapter)


@pytest.mark.asyncio
async def test_a_phantom_of_another_symbol_does_not_defer_the_cancel(tmp_config):
    adapter = _adapter(tmp_config)
    _register(adapter, _intent(4173, symbol="TMFK6"))

    ok = await adapter._dispatch_to_api(_cancel("158DFA"))

    assert ok is False
    adapter._add_to_dlq.assert_awaited_once()

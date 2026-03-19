"""Tests for HALT auto-cancel live orders (WU-08)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hft_platform.contracts.strategy import IntentType, StormGuardState
from hft_platform.order.halt_canceller import cancel_all_live_orders, halt_cancel_orders_total


def _make_adapter(live_orders: dict | None = None) -> SimpleNamespace:
    """Build a minimal mock OrderAdapter."""
    return SimpleNamespace(
        live_orders=live_orders or {},
        _live_orders_lock=asyncio.Lock(),
        order_queue=asyncio.Queue(maxsize=1024),
    )


def _make_storm_guard(state: StormGuardState) -> SimpleNamespace:
    return SimpleNamespace(state=state)


@pytest.mark.asyncio
async def test_cancels_all_live_orders() -> None:
    orders = {
        "strat_a:1001": {"status": "open"},
        "strat_b:2002": {"status": "open"},
        "strat_a:3003": {"status": "open"},
    }
    adapter = _make_adapter(orders)
    sg = _make_storm_guard(StormGuardState.HALT)

    count = await cancel_all_live_orders(adapter, sg)

    assert count == 3
    assert adapter.order_queue.qsize() == 3

    # Verify all enqueued commands are CANCEL
    while not adapter.order_queue.empty():
        cmd = adapter.order_queue.get_nowait()
        assert cmd.intent.intent_type == IntentType.CANCEL
        assert cmd.intent.reason == "HALT_AUTO_CANCEL"
        assert cmd.storm_guard_state == StormGuardState.HALT


@pytest.mark.asyncio
async def test_empty_dict_noop() -> None:
    adapter = _make_adapter({})
    sg = _make_storm_guard(StormGuardState.HALT)

    count = await cancel_all_live_orders(adapter, sg)

    assert count == 0
    assert adapter.order_queue.empty()


@pytest.mark.asyncio
async def test_only_proceeds_in_halt() -> None:
    orders = {"strat_a:1001": {"status": "open"}}
    adapter = _make_adapter(orders)

    for state in (StormGuardState.NORMAL, StormGuardState.WARM, StormGuardState.STORM):
        sg = _make_storm_guard(state)
        count = await cancel_all_live_orders(adapter, sg)
        assert count == 0
        assert adapter.order_queue.empty()


@pytest.mark.asyncio
async def test_idempotent() -> None:
    """Calling twice with same snapshot should enqueue cancels both times (idempotent)."""
    orders = {"strat_a:100": {"status": "open"}}
    adapter = _make_adapter(orders)
    sg = _make_storm_guard(StormGuardState.HALT)

    c1 = await cancel_all_live_orders(adapter, sg)
    c2 = await cancel_all_live_orders(adapter, sg)

    assert c1 == 1
    assert c2 == 1
    assert adapter.order_queue.qsize() == 2


@pytest.mark.asyncio
async def test_metric_incremented() -> None:
    orders = {"strat_x:42": {"status": "open"}}
    adapter = _make_adapter(orders)
    sg = _make_storm_guard(StormGuardState.HALT)

    before = halt_cancel_orders_total._value.get()
    await cancel_all_live_orders(adapter, sg)
    after = halt_cancel_orders_total._value.get()

    assert after - before == 1

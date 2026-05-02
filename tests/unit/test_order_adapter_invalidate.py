"""Tests for OrderAdapter.invalidate_live_orders() — post-reconnect cleanup."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.order.adapter import OrderAdapter


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def _make_adapter(mock_load: MagicMock) -> OrderAdapter:
    queue: asyncio.Queue = asyncio.Queue()
    client = MagicMock()
    return OrderAdapter("config/dummy.yaml", queue, client)


@pytest.mark.asyncio
async def test_invalidate_live_orders_clears_and_returns_count():
    adapter = _make_adapter()
    adapter.live_orders["strat:1"] = {"id": "T1"}
    adapter.live_orders["strat:2"] = {"id": "T2"}
    adapter._pending_order_keys.add("strat:1")

    count = await adapter.invalidate_live_orders(reason="reconnect")

    assert count == 2
    assert len(adapter.live_orders) == 0
    assert len(adapter._pending_order_keys) == 0


@pytest.mark.asyncio
async def test_invalidate_live_orders_empty_returns_zero():
    adapter = _make_adapter()

    count = await adapter.invalidate_live_orders(reason="test")

    assert count == 0
    assert len(adapter.live_orders) == 0


@pytest.mark.asyncio
async def test_invalidate_live_orders_logs_first_10_keys():
    adapter = _make_adapter()
    for i in range(15):
        adapter.live_orders[f"strat:{i}"] = {"id": f"T{i}"}

    count = await adapter.invalidate_live_orders(reason="session_rollover")

    assert count == 15
    assert len(adapter.live_orders) == 0

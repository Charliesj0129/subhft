"""Tests for order drain with timeout (WU-02)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _make_order_adapter():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as m,
        patch("hft_platform.order.adapter.LatencyRecorder") as lr,
        patch("hft_platform.order.adapter.yaml") as y,
        patch("builtins.open", MagicMock()),
    ):
        m.get.return_value = MagicMock()
        lr.get.return_value = MagicMock()
        y.safe_load.return_value = {}
        from hft_platform.order.adapter import OrderAdapter

        client = MagicMock()
        client.cancel_order = MagicMock(return_value=True)
        return OrderAdapter("config/base/order_adapter.yaml", asyncio.Queue(), client)


class TestOrderDrain:
    @pytest.mark.asyncio
    async def test_drain_cancels_live_orders(self):
        adapter = _make_order_adapter()
        adapter.live_orders["s:1"] = MagicMock()
        adapter.live_orders["s:2"] = MagicMock()
        cancelled = await adapter.drain_and_cancel(timeout_s=2.0)
        assert cancelled == 2

    @pytest.mark.asyncio
    async def test_drain_no_orders_returns_zero(self):
        adapter = _make_order_adapter()
        cancelled = await adapter.drain_and_cancel()
        assert cancelled == 0

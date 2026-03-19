import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _make():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as m,
        patch("hft_platform.order.adapter.LatencyRecorder") as l,
        patch("hft_platform.order.adapter.yaml") as y,
        patch("builtins.open", MagicMock()),
    ):
        m.get.return_value = MagicMock()
        l.get.return_value = MagicMock()
        y.safe_load.return_value = {}
        from hft_platform.order.adapter import OrderAdapter

        return OrderAdapter(
            "config/base/order_adapter.yaml", asyncio.Queue(), MagicMock(cancel_order=MagicMock(return_value=True))
        )


class TestDrain:
    @pytest.mark.asyncio
    async def test_cancels(self):
        a = _make()
        a.live_orders["s:1"] = MagicMock()
        a.live_orders["s:2"] = MagicMock()
        assert await a.drain_and_cancel(2.0) == 2

    @pytest.mark.asyncio
    async def test_empty(self):
        assert await _make().drain_and_cancel() == 0

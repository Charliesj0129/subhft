import asyncio
from unittest.mock import MagicMock, patch
import pytest
def _make():
    with patch("hft_platform.order.adapter.MetricsRegistry") as m, \
         patch("hft_platform.order.adapter.LatencyRecorder") as l, \
         patch("hft_platform.order.adapter.yaml") as y, patch("builtins.open", MagicMock()):
        m.get.return_value = MagicMock(); l.get.return_value = MagicMock(); y.safe_load.return_value = {}
        from hft_platform.order.adapter import OrderAdapter
        return OrderAdapter("config/base/order_adapter.yaml", asyncio.Queue(), MagicMock())
class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_empties_queue(self):
        a = _make()
        for _ in range(3): await a.order_queue.put(MagicMock())
        assert a.order_queue.qsize() == 3
        await a.drain_and_cancel(0.5)
        assert a.order_queue.empty()
    @pytest.mark.asyncio
    async def test_drain_returns_zero_when_no_orders(self):
        a = _make()
        r = await a.drain_and_cancel(0.5)
        assert r == 0

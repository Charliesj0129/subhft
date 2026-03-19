import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

def _make():
<<<<<<< Updated upstream
    with patch("hft_platform.order.adapter.MetricsRegistry") as m, \
         patch("hft_platform.order.adapter.LatencyRecorder") as lr, \
         patch("hft_platform.order.adapter.yaml") as y, patch("builtins.open", MagicMock()):
        m.get.return_value = MagicMock(); l.get.return_value = MagicMock(); y.safe_load.return_value = {}
=======
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as m,
        patch("hft_platform.order.adapter.LatencyRecorder") as lr,
        patch("hft_platform.order.adapter.yaml") as y,
        patch("builtins.open", MagicMock()),
    ):
        m.get.return_value = MagicMock()
        lr.get.return_value = MagicMock()
        y.safe_load.return_value = {}
>>>>>>> Stashed changes
        from hft_platform.order.adapter import OrderAdapter
        return OrderAdapter("config/base/order_adapter.yaml", asyncio.Queue(), MagicMock(cancel_order=MagicMock(return_value=True)))

class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_empties_queue(self):
        a = _make()
        for _ in range(3):
            await a.order_queue.put(MagicMock())
        assert a.order_queue.qsize() == 3
        await a.drain_and_cancel(2.0)
        assert a.order_queue.empty()

    @pytest.mark.asyncio
    async def test_drain_no_orders(self):
        a = _make()
        result = await a.drain_and_cancel()
        assert result == 0

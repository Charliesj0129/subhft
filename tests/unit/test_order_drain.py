"""Tests for order drain with timeout (WU-02)."""
import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import yaml


def _make():
    config = {}
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(config, tmp)
    tmp.close()
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as m,
        patch("hft_platform.order.adapter.LatencyRecorder") as lr,
    ):
        m.get.return_value = MagicMock()
        lr.get.return_value = MagicMock()
        from hft_platform.order.adapter import OrderAdapter
        adapter = OrderAdapter(tmp.name, asyncio.Queue(), MagicMock(cancel_order=MagicMock(return_value=True)))
        os.unlink(tmp.name)
        return adapter


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_empties_queue(self):
        a = _make()
        for _ in range(3):
            await a.order_queue.put(MagicMock())
        assert a.order_queue.qsize() == 3
        await a.drain_and_cancel(0.5)
        assert a.order_queue.empty()

    @pytest.mark.asyncio
    async def test_drain_no_live_orders_returns_zero(self):
        a = _make()
        cancelled = await a.drain_and_cancel(0.5)
        assert cancelled == 0

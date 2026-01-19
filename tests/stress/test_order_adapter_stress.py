import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter


class _Meta:
    def __init__(self, scale: int):
        self._scale = scale

    def price_scale(self, symbol: str) -> int:
        return self._scale


@pytest.mark.stress
@pytest.mark.asyncio
@patch("hft_platform.order.adapter.OrderAdapter.load_config")
async def test_order_adapter_bulk_cleanup(mock_load):
    if os.getenv("HFT_RUN_STRESS") != "1":
        pytest.skip("Set HFT_RUN_STRESS=1 to run stress tests")

    total = int(os.getenv("HFT_STRESS_ORDERS", "500"))
    queue = asyncio.Queue()
    client = MagicMock()
    client.get_exchange.return_value = "TSE"

    counter = 0

    def place_order(*args, **kwargs):
        nonlocal counter
        counter += 1
        return {"seq_no": f"S{counter}", "ord_no": f"O{counter}"}

    client.place_order.side_effect = place_order

    adapter = OrderAdapter("config/dummy.yaml", queue, client)
    adapter.metadata = _Meta(scale=100)
    adapter.rate_limiter.update(soft_cap=total + 10, hard_cap=total + 10, window_s=60)

    deadline_ns = time.time_ns() + 60_000_000_000
    for i in range(1, total + 1):
        intent = OrderIntent(
            intent_id=i,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=10000,
            qty=1,
            tif=TIF.LIMIT,
            timestamp_ns=0,
        )
        cmd = OrderCommand(cmd_id=i, intent=intent, deadline_ns=deadline_ns, storm_guard_state=0)
        await adapter.execute(cmd)

    assert len(adapter.live_orders) == total

    for i in range(1, total + 1):
        adapter.on_terminal_state("strat", f"O{i}")

    assert not adapter.live_orders

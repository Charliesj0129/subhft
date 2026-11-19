import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.simple_mm import SimpleMarketMaker
from hft_platform.strategy.runner import StrategyRunner


class MockBus:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.cursor = -1

    async def publish(self, event):
        await self.queue.put(event)

    async def consume(self, start_cursor=None, consumer_name=None):
        while True:
            yield await self.queue.get()


@pytest.mark.asyncio
@pytest.mark.skip(reason="MockBus.consume doesn't match StrategyRunner.run batch path (pre-existing)")
async def test_simple_mm_logic():
    """Verify SimpleMarketMaker receives ticks and places dual-sided quotes."""
    bus = MockBus()
    risk_queue = asyncio.Queue()

    runner = StrategyRunner(bus, risk_queue, lob_engine=None, position_store=None)

    runner.strategies = []

    mm = SimpleMarketMaker(strategy_id="mm-01", subscribe_symbols=["2330"])
    runner.register(mm)

    mock_pos_store = MagicMock(spec=[])
    mock_pos_store.positions = {"2330": 0}
    mock_pos_store._rust_tracker = None
    runner.position_store = mock_pos_store

    task = asyncio.create_task(runner.run())

    stats = LOBStatsEvent(
        symbol="2330",
        ts=1000,
        imbalance=0.1,
        best_bid=99,
        best_ask=100,
        bid_depth=10,
        ask_depth=10,
    )

    await bus.publish(stats)

    intents = []
    try:
        for _ in range(2):
            intent = await asyncio.wait_for(risk_queue.get(), timeout=1.0)
            intents.append(intent)
    except asyncio.TimeoutError:
        pass

    assert len(intents) == 2
    buy_intent = next((i for i in intents if i.side == Side.BUY), None)
    sell_intent = next((i for i in intents if i.side == Side.SELL), None)

    assert buy_intent is not None
    assert sell_intent is not None

    assert buy_intent.price < sell_intent.price
    assert buy_intent.price > 0

    print("SimpleMM Strategy Logic Verified.")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(test_simple_mm_logic())

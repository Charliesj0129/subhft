
import pytest
import asyncio
from unittest.mock import MagicMock
from hft_platform.strategy.runner import StrategyRunner
from hft_platform.strategies.simple_mm import SimpleMarketMaker
from hft_platform.contracts.strategy import Side

# Mock Bus
class MockBus:
    def __init__(self):
        self.queue = asyncio.Queue()
    async def publish(self, event):
        await self.queue.put(event)
    async def consume(self):
        while True:
            yield await self.queue.get()

@pytest.mark.asyncio
async def test_simple_mm_logic():
    """Verify SimpleMarketMaker receives ticks and places dual-sided quotes."""
    bus = MockBus()
    risk_queue = asyncio.Queue()
    
    # 1. Setup Runner & Strategy
    runner = StrategyRunner(bus, risk_queue, lob_engine=None, position_store=None)
    
    # Override registry loading to manual registration
    runner.strategies = [] 
    
    mm = SimpleMarketMaker(strategy_id="mm-01", subscribe_symbols=["2330"])
    runner.register(mm)
    
    # Inject fake position into context by mocking the loader?
    # Runner creates context on fly using self.position_store.
    # We can mock position_store.
    mock_pos_store = MagicMock()
    mock_pos_store.positions = {"2330": 0} # Flat position
    runner.position_store = mock_pos_store
    
    # Run Runner in background
    task = asyncio.create_task(runner.run())
    
    # 2. Publish Tick (Scaled x10000)
    # mid=100.0 -> 1,000,000; spread=1.0 -> 10,000
    tick = {"symbol": "2330", "mid_price": 1000000, "spread": 10000}
    await bus.publish(tick)
    
    # 3. Wait for Order Intents
    # Expect 2 headers (Buy/Sell)
    # SimpleMM places 1 buy, 1 sell per tick if pos within limits.
    
    intents = []
    try:
        # Wait for 2 intents
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
    # Price is scaled (x100 or x1 implied by 9750 vs 100.0)
    # Check relative structure: Bid < Ask
    assert buy_intent.price < sell_intent.price
    # Check absolute sanity
    assert buy_intent.price > 0
    assert sell_intent.price > buy_intent.price
    
    # 4. Filter Logic (Private Events)
    # Send Fill for "mm-01" -> Should be processed
    # Send Fill for "other-strat" -> Should be ignored
    
    # Need to spy on mm.on_fill
    mm.on_fill = MagicMock()
    
    fill_ok = {"topic": "deal", "strategy_id": "mm-01", "symbol": "2330", "qty": 1, "price": 99}
    fill_ignore = {"topic": "deal", "strategy_id": "other-strat", "symbol": "2330", "qty": 1}
    
    await bus.publish(fill_ok)
    await bus.publish(fill_ignore)
    
    await asyncio.sleep(0.1)
    
    # Check calls
    assert mm.on_fill.call_count == 1
    call_args = mm.on_fill.call_args[0][0]
    assert call_args["strategy_id"] == "mm-01"
    
    # Cleanup
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    print("SimpleMM Strategy Logic Verified.")

if __name__ == "__main__":
    asyncio.run(test_simple_mm_logic())

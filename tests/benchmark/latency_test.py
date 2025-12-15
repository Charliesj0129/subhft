
import asyncio
import time
from unittest.mock import MagicMock

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner
from hft_platform.feed_adapter.adapter import FeedAdapter
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.contracts.strategy import Side, TIF

class BenchStrategy(BaseStrategy):
    def __init__(self, strategy_id="bench"):
        super().__init__(strategy_id)
        self.count = 0
        self.start_times = []
        
    def on_lob(self, lob: dict, metadata: dict):
        self.count += 1
        # Simple logic overhead
        mid = (lob["bids"][0][0] + lob["asks"][0][0]) / 2
        self.place_limit_order("2330", Side.BUY, int(mid), 1)

async def benchmark():
    print("=== HFT Platform Efficiency Benchmark ===")
    
    # 1. Setup
    bus = RingBufferBus(size=65536)
    lob_engine = LOBEngine()
    client = MagicMock()
    client.place_order.return_value = {"status": "Submitted"}
    
    raw_queue = asyncio.Queue()
    normalizer = MarketDataNormalizer()
    feed = FeedAdapter(client, lob_engine, bus, raw_queue, normalizer)
    
    risk_queue = asyncio.Queue()
    strat = BenchStrategy()
    strat.symbols = ["2330"]
    strat.enabled = True
    
    runner = StrategyRunner(bus, risk_queue, lob_engine=lob_engine)
    runner.strategies = [strat]
    
    # 2. Start Services
    feed.running = True
    feed_loop = asyncio.create_task(feed._consume_loop())
    runner_loop = asyncio.create_task(runner.run())
    
    # Allow spinning up
    await asyncio.sleep(1.0)
    print("Services Started. Pre-warming...")
    
    # 3. Pre-warm (ensure subscriptions active)
    for _ in range(100):
        raw_queue.put_nowait({
            "code": "2330", "ts": 1000, 
            "bid_price": [100], "bid_volume": [1], 
            "ask_price": [101], "ask_volume": [1]
        })
    
    await asyncio.sleep(1.0)
    print(f"Pre-warm complete. Strat processed: {strat.count}")
    strat.count = 0 # Reset
    
    # 4. Burst Test
    NUM = 50000
    print(f"Injecting {NUM} events...")
    
    start_t = time.perf_counter()
    
    for i in range(NUM):
        msg = {
            "code": "2330", 
            "ts": 1000 + i, 
            "bid_price": [100 + i*0.01], "bid_volume": [50], 
            "ask_price": [101 + i*0.01], "ask_volume": [50]
        }
        raw_queue.put_nowait(msg)
        
    # Wait for drain
    # Feed drains queue -> Bus -> Runner -> Strat
    
    while strat.count < NUM:
        await asyncio.sleep(0.01)
        if time.perf_counter() - start_t > 15.0:
            print("Timeout!")
            break
            
    dur = time.perf_counter() - start_t
    tput = strat.count / dur
    
    print(f"\nResult: Processed {strat.count}/{NUM} in {dur:.4f}s")
    print(f"Throughput: {tput:.2f} events/sec")
    print(f"Latency per event: {1_000_000/tput:.2f} µs")
    
    # Cleanup
    feed.running = False
    runner.running = False
    feed_loop.cancel()
    runner_loop.cancel()

if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging
    configure_logging() # Optional
    asyncio.run(benchmark())

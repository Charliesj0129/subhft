import asyncio
import time
from unittest.mock import MagicMock

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.services.market_data import MarketDataService
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner


class BenchStrategy(BaseStrategy):
    def __init__(self, strategy_id="bench"):
        super().__init__(strategy_id)
        self.count = 0
        self.start_times = []

    def on_stats(self, event):
        self.count += 1
        now = time.time_ns()
        latency = now - event.ts
        self.start_times.append(latency)
        mid = event.mid_price
        self.buy("2330", int(mid), 1)


async def benchmark():
    print("=== HFT Platform Efficiency Benchmark ===")

    # 1. Setup
    bus = RingBufferBus(size=65536)
    lob_engine = LOBEngine()
    client = MagicMock()
    client.place_order.return_value = {"status": "Submitted"}

    raw_queue = asyncio.Queue()
    raw_queue = asyncio.Queue()
    # Mock client with valid symbols for normalization if needed
    client = MagicMock(spec=ShioajiClient)
    client.symbols = [{"code": "2330", "price_scale": 1, "exchange": "TSE"}]

    md_service = MarketDataService(bus, raw_queue, client)
    md_service.normalizer = MarketDataNormalizer()  # Ensure fresh
    # Assign same LOB engine if needed for shared access test?
    md_service.lob = lob_engine

    risk_queue = asyncio.Queue()
    strat = BenchStrategy()
    strat.symbols = ["2330"]
    strat.enabled = True

    runner = StrategyRunner(bus, risk_queue, lob_engine=lob_engine)
    runner.register(strat)

    # 2. Start Services
    # 2. Start Services
    md_service.running = True
    # Consume loop is integrated in run() for Service, but that blocks.
    # Service structure: run() spawns monitor, then connect, then loop.
    # We want pure process loop.
    # Let's override run to just consume to avoid connect sequence overhead/errors in benchmark?
    # Or start properly.

    # Let's bypass connect sequence for benchmark purity:
    async def fast_run():
        md_service.running = True
        while md_service.running:
            # Inline logic from service
            item = await md_service.raw_queue.get()
            # We want to use the REAL logic though.
            # Call internal methods/libs?
            # Actually, simpler to just run md_service.run() but we need to mock connect sequence to be instant.
            pass

    # Better: Just spawn md_service.run() and ensure client mocks don't hang.
    client.login.return_value = None
    client.fetch_snapshots.return_value = []
    client.subscribe_basket.return_value = None

    feed_loop = asyncio.create_task(md_service.run())
    runner_loop = asyncio.create_task(runner.run())

    # Allow spinning up
    await asyncio.sleep(1.0)
    print("Services Started. Pre-warming...")

    # 3. Pre-warm (ensure subscriptions active)
    for _ in range(100):
        raw_queue.put_nowait(
            {"code": "2330", "ts": 1000, "bid_price": [100], "bid_volume": [1], "ask_price": [101], "ask_volume": [1]}
        )

    print(f"Pre-warm complete. Strat processed: {strat.count}")
    strat.count = 0  # Reset

    # 4. Burst Test
    NUM = 10000
    print(f"Injecting {NUM} events...")

    start_t = time.perf_counter()

    for i in range(NUM):
        msg = {
            "code": "2330",
            "ts": time.time_ns(),
            "bid_price": [100 + i * 0.01],
            "bid_volume": [50],
            "ask_price": [101 + i * 0.01],
            "ask_volume": [50],
        }
        raw_queue.put_nowait(msg)
        # Yield to allow consumer to pick up?
        # If we just blast, queue fills up.
        # RingBuffer has 65k size. NUM=10k. It fits.

    while strat.count < NUM:
        await asyncio.sleep(0.01)
        if time.perf_counter() - start_t > 15.0:
            print("Timeout!")
            break

    dur = time.perf_counter() - start_t
    tput = strat.count / dur

    print(f"\nResult: Processed {strat.count}/{NUM} in {dur:.4f}s")
    print(f"Throughput: {tput:.2f} events/sec")

    if strat.start_times:
        latencies = sorted(strat.start_times)
        p50 = latencies[int(len(latencies) * 0.50)] / 1000.0
        p95 = latencies[int(len(latencies) * 0.95)] / 1000.0
        p99 = latencies[int(len(latencies) * 0.99)] / 1000.0
        print(f"Latency P50: {p50:.2f} us")
        print(f"Latency P95: {p95:.2f} us")
        print(f"Latency P99: {p99:.2f} us")

    # Cleanup
    md_service.running = False
    runner.running = False
    feed_loop.cancel()
    runner_loop.cancel()


if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging

    configure_logging()  # Optional
    asyncio.run(benchmark())

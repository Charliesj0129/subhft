import asyncio
import time

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer


async def benchmark_pipeline():
    bus = RingBufferBus(size=200000)  # Increased size for benchmark
    lob = LOBEngine()
    norm = MarketDataNormalizer()

    # Mock event
    raw_event = {"code": "2330", "ts": time.time_ns(), "close": 1000.0, "volume": 5, "tick_type": 1}

    count = 100000
    consumed = 0

    async def _consumer():
        nonlocal consumed
        async for batch in bus.consume_batch(batch_size=256, start_cursor=-1):
            consumed += len(batch)
            if consumed >= count:
                break

    consumer_task = asyncio.create_task(_consumer())
    await asyncio.sleep(0)

    start = time.time()
    for _ in range(count):
        event = norm.normalize_tick(raw_event)
        lob.process_event(event)
        bus.publish_nowait(event)
    await consumer_task
    duration = time.time() - start
    print(f"Processed {count} events in {duration:.4f}s")
    print(f"Throughput: {count / duration:.2f} events/sec")
    print(f"Average Latency: {duration * 1000000 / count:.2f} us")


if __name__ == "__main__":
    asyncio.run(benchmark_pipeline())


import asyncio
import time
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

async def benchmark_pipeline():
    bus = RingBufferBus(size=200000) # Increased size for benchmark
    lob = LOBEngine()
    norm = MarketDataNormalizer()
    consumer_queue = asyncio.Queue(maxsize=200000)
    bus.subscribers.append(consumer_queue)
    
    # Mock event
    raw_event = {
         "code": "2330",
         "ts": time.time_ns(),
         "close": 1000.0,
         "volume": 5,
         "tick_type": 1
    }
    
    count = 100000
    start = time.time()
    
    # We must consume elements to prevent full queue
    # Or just increase size for this micro-benchmark
    
    for _ in range(count):
        # 1. Normalize
        event = norm.normalize_tick(raw_event)
        # 2. LOB Update
        lob.process_event(event)
        # 3. Publish
        bus.publish(event)
        # Drain immediately for benchmark to avoid backlog
        consumer_queue.get_nowait()
        consumer_queue.task_done()
        
    duration = time.time() - start
    print(f"Processed {count} events in {duration:.4f}s")
    print(f"Throughput: {count/duration:.2f} events/sec")
    print(f"Average Latency: {duration*1000000/count:.2f} us")

if __name__ == "__main__":
    asyncio.run(benchmark_pipeline())

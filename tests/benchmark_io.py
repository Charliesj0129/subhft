
import asyncio
import time
import shutil
import os
from structlog import get_logger

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.recorder.wal import WALWriter

logger = get_logger("benchmark")

async def test_wal_latency():
    print("\n--- Testing WAL Latency ---")
    wal_dir = "/tmp/bench_wal"
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir)
        
    wal = WALWriter(wal_dir)
    data = [{"ts": time.time_ns(), "val": i} for i in range(100)]
    
    # Measure time to fire-and-forget 100 writes
    start = time.perf_counter()
    tasks = []
    for i in range(100):
        tasks.append(wal.write("bench_table", data))
    
    fire_duration = time.perf_counter() - start
    print(f"Fire-and-forget 100 batches took: {fire_duration*1000:.2f} ms")
    
    await asyncio.gather(*tasks)
    total_duration = time.perf_counter() - start
    print(f"Total time (including threads): {total_duration*1000:.2f} ms")
    
    # Verify files
    files = os.listdir(wal_dir)
    print(f"Files created: {len(files)} (Expected 100)")
    assert len(files) == 100
    
async def test_bus_throughput():
    print("\n--- Testing Event Bus Throughput ---")
    bus = RingBufferBus(size=16384)
    
    received_count = 0
    
    async def consumer():
        nonlocal received_count
        async for event in bus.consume(start_cursor=-1):
            received_count += 1
            if event == "STOP":
                break
    
    consumer_task = asyncio.create_task(consumer())
    
    start = time.perf_counter()
    N = 10000
    for i in range(N):
        await bus.publish(i)
    await bus.publish("STOP")
    
    await consumer_task
    duration = time.perf_counter() - start
    
    print(f"Published {N} events in {duration*1000:.2f} ms")
    print(f"Throughput: {N/duration:.0f} events/sec")
    assert received_count == N + 1 # + STOP

if __name__ == "__main__":
    async def main():
        await test_wal_latency()
        await test_bus_throughput()
    
    asyncio.run(main())

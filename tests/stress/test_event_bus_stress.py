import asyncio
import os
import time

import pytest

from hft_platform.engine.event_bus import RingBufferBus


@pytest.mark.stress
@pytest.mark.asyncio
async def test_event_bus_throughput():
    if os.getenv("HFT_RUN_STRESS") != "1":
        pytest.skip("Set HFT_RUN_STRESS=1 to run stress tests")

    bus = RingBufferBus(size=8192)
    total = int(os.getenv("HFT_STRESS_EVENTS", "5000"))
    received = 0

    async def consumer():
        nonlocal received
        async for _ in bus.consume(start_cursor=-1):
            received += 1
            if received >= total:
                break

    task = asyncio.create_task(consumer())
    start = time.time()

    for i in range(total):
        await bus.publish({"seq": i})

    await asyncio.wait_for(task, timeout=5.0)
    elapsed = time.time() - start

    assert received == total
    assert elapsed < 5.0


@pytest.mark.stress
@pytest.mark.asyncio
async def test_event_bus_multi_consumer():
    if os.getenv("HFT_RUN_STRESS") != "1":
        pytest.skip("Set HFT_RUN_STRESS=1 to run stress tests")

    bus = RingBufferBus(size=4096)
    total = int(os.getenv("HFT_STRESS_EVENTS", "3000"))
    counts = [0, 0]

    async def consumer(idx):
        async for _ in bus.consume(start_cursor=-1):
            counts[idx] += 1
            if counts[idx] >= total:
                break

    tasks = [asyncio.create_task(consumer(0)), asyncio.create_task(consumer(1))]

    for i in range(total):
        await bus.publish({"seq": i})

    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    assert counts == [total, total]


@pytest.mark.stress
@pytest.mark.asyncio
async def test_event_bus_overflow_skips_to_latest():
    if os.getenv("HFT_RUN_STRESS") != "1":
        pytest.skip("Set HFT_RUN_STRESS=1 to run stress tests")

    bus = RingBufferBus(size=3)
    for i in range(10):
        await bus.publish({"seq": i})

    received = []

    async def consumer():
        async for event in bus.consume(start_cursor=-1):
            received.append(event["seq"])
            if len(received) >= 3:
                break

    await asyncio.wait_for(consumer(), timeout=2.0)

    assert received == [7, 8, 9]

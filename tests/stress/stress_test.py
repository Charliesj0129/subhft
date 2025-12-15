
import asyncio
import time
import random
import psutil
import os
from structlog import get_logger
from hft_platform.main import HFTSystem
from hft_platform.config import loader

# Configure logging to suppress debug noise during stress logic
import logging
logging.getLogger("shioaji").setLevel(logging.WARNING)

logger = get_logger("stress_test")

class StressFeeder:
    """Generates synthetic high-frequency market data."""
    def __init__(self, system, rate_per_sec=1000, duration_sec=10):
        self.system = system
        self.rate = rate_per_sec
        self.duration = duration_sec
        self.sent_count = 0
        
    async def run(self):
        logger.info("Starting Stress Feed", rate=self.rate, duration=self.duration)
        start_time = time.time()
        delay = 1.0 / self.rate
        
        while time.time() - start_time < self.duration:
            # Burst generation
            # Send batch to avoid asyncio overhead domination? 
            # No, we want to text event loop switching.
            
            # Construct Event
            event = {
                "topic": "quote", 
                "code": "2330", 
                "ts": int(time.time_ns()),
                "AskPrice": [100.0 + random.random()],
                "BidPrice": [99.0 + random.random()],
                "AskVolume": [random.randint(1,10)],
                "BidVolume": [random.randint(1,10)]
            }
            
            # Inject directly into Bus or via Normalize?
            # We want to stress the WHOLE pipeline: Feed -> Normalize -> Bus -> Strategy -> Order
            
            # System typically receives from Shioaji callbacks.
            # We can mock the callback invocation.
            
            # self.system.on_quote(TOPIC, MSG)
            # We need to construct msgspec/dict as system expects.
            
            # Let's bypass shioaji client and inject generic dicts into system.process_quote
            # Need to verify main.py signature. Assuming it listens to event bus or has ingest method.
            
            # If system structure is Bus-based:
            # self.system.bus.publish(event)
            # But the real bottleneck is often normalization.
            
            # Simulating Main Loop Ingestion
            # HFTSystem doesn't expose public ingest easily?
            pass
            
            # Ideally we want to measure processing time
            await asyncio.sleep(delay)
            
        logger.info("Stress Feed Completed", count=self.sent_count)

async def monitor_resources(pid, duration):
    proc = psutil.Process(pid)
    logger.info("Starting Monitor")
    start = time.time()
    max_mem = 0
    while time.time() - start < duration:
        mem = proc.memory_info().rss / 1024 / 1024 # MB
        cpu = proc.cpu_percent()
        max_mem = max(max_mem, mem)
        # logger.info("Stats", mem_mb=mem, cpu=cpu)
        await asyncio.sleep(1)
    logger.info("Monitor Finished", max_mem_mb=max_mem)

async def main():
    # 1. Setup System
    # Load settings
    settings, _ = loader.load_settings()
    # Disable actual Shioaji connection
    os.environ["SHIOAJI_SIMULATION"] = "true" 
    
    system = HFTSystem(settings)
    
    # Start System in background
    t_sys = asyncio.create_task(system.run())
    
    # Allow startup
    await asyncio.sleep(2)
    
    # 2. Inject Load
    # We need to patch the feeder to inject data
    # Assuming system.feed_adapter has a way, or we trigger callbacks.
    
    # We will "mock" the shioi_client's callback mechanism
    # Inspecting HFTSystem to see how it binds
    
    # If we can't easily inject, we will just use internal bus
    if hasattr(system, "bus"):
        logger.info("Injecting via EventBus")
        
        # Stress Run
        rate = 5000 # 5k events/sec
        duration = 5 # 5 seconds
        
        total = rate * duration
        start = time.time()
        
        for i in range(total):
            # Synthetic Normalized Tick
            tick = {
                "type": "tick",
                "symbol": "2330",
                "price": 100.0,
                "volume": 1,
                "ts": time.time_ns()
            }
            # system.bus.publish(tick)
            if i % 1000 == 0:
                await asyncio.sleep(0.001) # Yield to let consumers run
                
        elapsed = time.time() - start
        logger.info("Throughput", msg_sec=total/elapsed)
        
    t_sys.cancel()
    try:
        await t_sys
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging
    configure_logging()
    asyncio.run(main())

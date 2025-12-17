import asyncio
from structlog import get_logger
from hft_platform.recorder.batcher import Batcher

logger = get_logger("recorder")

from hft_platform.recorder.writer import DataWriter

class RecorderService:
    def __init__(self, queue: asyncio.Queue, clickhouse_client=None):
        self.queue = queue
        self.running = False
        
        # Init Writer
        self.writer = DataWriter()
        self.writer.connect()
        
        self.batchers = {
            "market_data": Batcher("market_data", writer=self.writer),
            "orders": Batcher("orders", writer=self.writer),         # Adjusted table name per schema
            "risk_log": Batcher("risk_log", writer=self.writer),
            "fills": Batcher("fills", writer=self.writer),
            # Add backtest routing if needed, or separate service
            "backtest_runs": Batcher("backtest_runs", writer=self.writer),
        }
        
    async def recover_wal(self):
        """Replay any unprocesed WAL files to ClickHouse on startup."""
        try:
            from hft_platform.recorder.loader import WALLoaderService
            # We use default config for now, assuming env vars set
            loader = WALLoaderService()
            loader.connect()
            if loader.ch_client:
                logger.info("Starting WAL Recovery...")
                # Run in thread to avoid blocking loop if heavy
                await asyncio.to_thread(loader.process_files)
                logger.info("WAL Recovery Complete")
            else:
                logger.warning("Skipping WAL Recovery (No ClickHouse Connection)")
        except Exception as e:
            logger.error("WAL Recovery Failed", error=str(e))

    async def run(self):
        self.running = True
        logger.info("Recorder started")
        
        # Attempt recovery
        await self.recover_wal()
        
        # Start flush ticker
        flush_task = asyncio.create_task(self._flush_loop())
        
        try:
            while self.running:
                item = await self.queue.get()
                topic = item.get("topic")
                data = item.get("data")
                
                if topic in self.batchers:
                    # Normalize moved to batcher or here?
                    # Ideally normalize BEFORE batching.
                    await self.batchers[topic].add(data)
                
                self.queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            flush_task.cancel()
            logger.info("Recorder stopped")

    async def _flush_loop(self):
        while self.running:
            await asyncio.sleep(0.1) # Check every 100ms
            for b in self.batchers.values():
                await b.check_flush()

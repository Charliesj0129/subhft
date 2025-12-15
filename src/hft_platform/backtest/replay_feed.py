import asyncio
from typing import List
from datetime import datetime
from structlog import get_logger

# from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.backtest.data_client import ClickHouseStreamer

logger = get_logger("replay_feed")

class ClickHouseReplayFeed:
    def __init__(self, bus, ch_client: ClickHouseStreamer, symbols: List[str], start_ts: datetime, end_ts: datetime):
        self.bus = bus
        self.ch_client = ch_client
        self.symbols = symbols
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.running = False
        
    async def run(self):
        """
        Stream events and push to bus.
        This is a generator producer. It blocks the event loop if we aren't careful,
        so we should yield control or run in a thread executor if deserialization is heavy.
        For now, standard async iteration.
        """
        self.running = True
        logger.info("Starting Replay", symbols=self.symbols, start=self.start_ts)
        
        count = 0
        
        # We need to run the blocking stream generator in a non-blocking way or assume fast fetch
        # Since stream_market_data does synchronous network IO (clickhouse-connect), 
        # we realistically need to run it in a separate thread and queue it, or use run_in_executor.
        # But for iteration simpler to just pre-fetch or use iterator in valid way.
        
        # Proper pattern for synchronous generator in async:
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=10000)
        
        def producer():
            try:
                for row in self.ch_client.stream_market_data(self.symbols, self.start_ts, self.end_ts):
                    if not self.running: break
                    
                    # Convert row to normalized dict
                    # Assuming row keys match what normalizer expected or we normalize here.
                    # Normalizer expects: {symbol, type, ...}
                    # We reconstruct:
                    norm = {
                        "symbol": row["symbol"],
                        "type": row["type"],
                        "exch_ts": int(row["exch_ts"].timestamp() * 1e9), # timestamp to ns or keep datetime? Spec says ns usually.
                        "recv_ts": int(row["ingest_ts"].timestamp() * 1e9),
                        # Maps needed ...
                    }
                    if row["type"] == "BidAsk":
                        norm["bids"] = [{"price": p, "volume": v} for p,v in zip(row["bids_price"], row["bids_vol"])]
                        norm["asks"] = [{"price": p, "volume": v} for p,v in zip(row["asks_price"], row["asks_vol"])]
                    elif row["type"] == "Tick":
                        norm["price"] = row["price"]
                        norm["volume"] = row["volume"]
                        
                    asyncio.run_coroutine_threadsafe(queue.put(norm), loop).result()
            except Exception as e:
                logger.error("Producer failed", error=str(e))
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result() # Sentinel

        # Start producer thread
        import threading
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        
        # Consume
        while self.running:
            event = await queue.get()
            if event is None:
                break
            
            # Rate limit simulation?
            # In purely event-driven backtest, we just push as fast as consumer can take.
            # StrategyRunner budget is CPU bound.
            
            await self.bus.publish(event)
            count += 1
            if count % 10000 == 0:
                logger.info("Replay Progress", count=count)
                # Yield to let others process
                await asyncio.sleep(0)
                
        logger.info("Replay Finished", total_events=count)

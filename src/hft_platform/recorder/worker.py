import asyncio
import os

from structlog import get_logger

from hft_platform.recorder.batcher import Batcher, GlobalMemoryGuard
from hft_platform.recorder.health import PipelineHealthTracker
from hft_platform.recorder.writer import DataWriter

logger = get_logger("recorder")


# ── CC-5: Schema extractors ──────────────────────────────────────────────


def _extract_market_data(row) -> dict | None:
    """Fast extractor for market_data events — bypasses generic serialize()."""
    try:
        return {
            "symbol": getattr(row, "symbol", None) or (row.get("symbol") if isinstance(row, dict) else None),
            "exchange": getattr(row, "exchange", None)
            or getattr(row, "exch", None)
            or (row.get("exchange", row.get("exch", "TSE")) if isinstance(row, dict) else "TSE"),
            "type": getattr(row, "type", None)
            or (row.get("type", "") if isinstance(row, dict) else ""),
            "exch_ts": getattr(row, "exch_ts", None)
            or getattr(row, "ts", None)
            or (row.get("exch_ts", row.get("ts")) if isinstance(row, dict) else None),
            "ingest_ts": getattr(row, "ingest_ts", None)
            or getattr(row, "recv_ts", None)
            or (row.get("ingest_ts", row.get("recv_ts")) if isinstance(row, dict) else None),
            "price_scaled": getattr(row, "price_scaled", None)
            or (row.get("price_scaled") if isinstance(row, dict) else None),
            "volume": getattr(row, "volume", None)
            or getattr(row, "total_volume", None)
            or (row.get("volume", row.get("total_volume", 0)) if isinstance(row, dict) else 0),
            "bids_price": getattr(row, "bids_price", None)
            or (row.get("bids_price") if isinstance(row, dict) else None),
            "bids_vol": getattr(row, "bids_vol", None)
            or (row.get("bids_vol") if isinstance(row, dict) else None),
            "asks_price": getattr(row, "asks_price", None)
            or (row.get("asks_price") if isinstance(row, dict) else None),
            "asks_vol": getattr(row, "asks_vol", None)
            or (row.get("asks_vol") if isinstance(row, dict) else None),
            "seq_no": getattr(row, "seq_no", None)
            or getattr(row, "seq", None)
            or (row.get("seq_no", row.get("seq", 0)) if isinstance(row, dict) else 0),
        }
    except Exception:
        return None


def _extract_order(row) -> dict | None:
    """Fast extractor for order events."""
    try:
        return {
            "order_id": getattr(row, "order_id", None)
            or (row.get("order_id") if isinstance(row, dict) else None),
            "strategy_id": getattr(row, "strategy_id", None)
            or (row.get("strategy_id") if isinstance(row, dict) else None),
            "symbol": getattr(row, "symbol", None)
            or (row.get("symbol") if isinstance(row, dict) else None),
            "exchange": getattr(row, "exchange", None)
            or (row.get("exchange", row.get("exch", "")) if isinstance(row, dict) else ""),
            "side": getattr(row, "side", None)
            or getattr(row, "action", None)
            or (row.get("side", row.get("action", "")) if isinstance(row, dict) else ""),
            "price_scaled": getattr(row, "price_scaled", None)
            or (row.get("price_scaled") if isinstance(row, dict) else None),
            "qty": getattr(row, "qty", None)
            or getattr(row, "quantity", None)
            or (row.get("qty", row.get("quantity", 0)) if isinstance(row, dict) else 0),
            "order_type": getattr(row, "order_type", None)
            or (row.get("order_type", row.get("type", "")) if isinstance(row, dict) else ""),
            "status": getattr(row, "status", None)
            or (row.get("status", "") if isinstance(row, dict) else ""),
            "exch_ts": getattr(row, "exch_ts", None)
            or (row.get("exch_ts", row.get("ts")) if isinstance(row, dict) else None),
            "ingest_ts": getattr(row, "ingest_ts", None)
            or (row.get("ingest_ts", row.get("recv_ts")) if isinstance(row, dict) else None),
        }
    except Exception:
        return None


def _extract_fill(row) -> dict | None:
    """Fast extractor for fill/trade events."""
    try:
        return {
            "trade_id": getattr(row, "trade_id", None)
            or getattr(row, "fill_id", None)
            or (row.get("trade_id", row.get("fill_id")) if isinstance(row, dict) else None),
            "order_id": getattr(row, "order_id", None)
            or (row.get("order_id") if isinstance(row, dict) else None),
            "symbol": getattr(row, "symbol", None)
            or (row.get("symbol") if isinstance(row, dict) else None),
            "exchange": getattr(row, "exchange", None)
            or (row.get("exchange", row.get("exch", "")) if isinstance(row, dict) else ""),
            "side": getattr(row, "side", None)
            or getattr(row, "action", None)
            or (row.get("side", row.get("action", "")) if isinstance(row, dict) else ""),
            "price_scaled": getattr(row, "price_scaled", None)
            or (row.get("price_scaled") if isinstance(row, dict) else None),
            "qty": getattr(row, "qty", None)
            or getattr(row, "quantity", None)
            or (row.get("qty", row.get("quantity", 0)) if isinstance(row, dict) else 0),
            "exch_ts": getattr(row, "exch_ts", None)
            or (row.get("exch_ts", row.get("ts")) if isinstance(row, dict) else None),
            "ingest_ts": getattr(row, "ingest_ts", None)
            or (row.get("ingest_ts", row.get("recv_ts")) if isinstance(row, dict) else None),
        }
    except Exception:
        return None


# Map of topic -> extractor function (CC-5)
_EXTRACTORS = {
    "market_data": _extract_market_data,
    "orders": _extract_order,
    "fills": _extract_fill,
}


class RecorderService:
    def __init__(self, queue: asyncio.Queue, clickhouse_client=None):
        self.queue = queue
        self.running = False

        # EC-5: Health tracker
        self.health_tracker = PipelineHealthTracker()

        # EC-1: Global memory guard
        self.memory_guard = GlobalMemoryGuard.get()
        self.memory_guard.set_health_tracker(self.health_tracker)

        # Init Writer
        self.writer = DataWriter()
        self.writer.set_health_tracker(self.health_tracker)

        # CC-5: Schema extractors enabled flag
        extract_enabled = os.getenv("HFT_BATCHER_SCHEMA_EXTRACT", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        self.batchers = {
            "market_data": Batcher(
                "hft.market_data",
                writer=self.writer,
                extractor=_EXTRACTORS.get("market_data") if extract_enabled else None,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "orders": Batcher(
                "hft.orders",
                writer=self.writer,
                extractor=_EXTRACTORS.get("orders") if extract_enabled else None,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "risk_log": Batcher(
                "hft.logs",
                writer=self.writer,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "fills": Batcher(
                "hft.trades",
                writer=self.writer,
                extractor=_EXTRACTORS.get("fills") if extract_enabled else None,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "backtest_runs": Batcher(
                "hft.backtest_runs",
                writer=self.writer,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "latency_spans": Batcher(
                "hft.latency_spans",
                writer=self.writer,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
        }

        # Register all batchers with memory guard
        for batcher in self.batchers.values():
            self.memory_guard.register(batcher)

    async def recover_wal(self):
        """Replay any unprocesed WAL files to ClickHouse on startup."""
        import os

        ch_enabled = str(os.getenv("HFT_CLICKHOUSE_ENABLED", "")).lower() in ("1", "true", "yes", "on")
        if os.getenv("HFT_DISABLE_CLICKHOUSE") or not ch_enabled:
            logger.info("Skipping WAL Recovery (ClickHouse disabled)")
            return

        try:
            from hft_platform.recorder.loader import WALLoaderService

            # We use default config for now, assuming env vars set
            loader = WALLoaderService()
            await asyncio.to_thread(loader.connect)
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

        await self.writer.connect_async()

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
            for batcher in self.batchers.values():
                await batcher.force_flush()
            # Graceful shutdown of writer (flush WAL batch, stop pool)
            await self.writer.shutdown()
            logger.info("Recorder stopped")

    async def _flush_loop(self):
        while self.running:
            await asyncio.sleep(0.1)  # Check every 100ms
            for b in self.batchers.values():
                await b.check_flush()

    def get_health(self) -> dict:
        """Return pipeline health status (EC-5)."""
        return self.health_tracker.get_health()

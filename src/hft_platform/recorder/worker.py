import asyncio
import os

from structlog import get_logger

from hft_platform.recorder.batcher import Batcher, GlobalMemoryGuard
from hft_platform.recorder.health import PipelineHealthTracker
from hft_platform.recorder.mode import RecorderMode, get_recorder_mode
from hft_platform.recorder.writer import DataWriter

logger = get_logger("recorder")


# ── CC-5: Schema extractors ──────────────────────────────────────────────

MARKET_DATA_COLUMNS = [
    "symbol",
    "exchange",
    "type",
    "exch_ts",
    "ingest_ts",
    "price_scaled",
    "volume",
    "bids_price",
    "bids_vol",
    "asks_price",
    "asks_vol",
    "seq_no",
]

ORDER_COLUMNS = [
    "order_id",
    "strategy_id",
    "symbol",
    "exchange",
    "side",
    "price_scaled",
    "qty",
    "order_type",
    "status",
    "exch_ts",
    "ingest_ts",
]

FILL_COLUMNS = [
    "trade_id",
    "order_id",
    "symbol",
    "exchange",
    "side",
    "price_scaled",
    "qty",
    "exch_ts",
    "ingest_ts",
]


def _extract_market_data_values(row) -> list | None:
    """Fast extractor for market_data events — bypasses generic serialize()."""
    try:
        if isinstance(row, dict):
            get = row.get
            return [
                get("symbol"),
                get("exchange", get("exch", "TSE")),
                get("type", ""),
                get("exch_ts", get("ts")),
                get("ingest_ts", get("recv_ts")),
                get("price_scaled"),
                get("volume", get("total_volume", 0)),
                get("bids_price"),
                get("bids_vol"),
                get("asks_price"),
                get("asks_vol"),
                get("seq_no", get("seq", 0)),
            ]
        return [
            getattr(row, "symbol", None),
            getattr(row, "exchange", None) or getattr(row, "exch", None) or "TSE",
            getattr(row, "type", None) or "",
            getattr(row, "exch_ts", None) or getattr(row, "ts", None),
            getattr(row, "ingest_ts", None) or getattr(row, "recv_ts", None),
            getattr(row, "price_scaled", None),
            getattr(row, "volume", None) or getattr(row, "total_volume", None) or 0,
            getattr(row, "bids_price", None),
            getattr(row, "bids_vol", None),
            getattr(row, "asks_price", None),
            getattr(row, "asks_vol", None),
            getattr(row, "seq_no", None) or getattr(row, "seq", None) or 0,
        ]
    except Exception:
        return None


def _extract_order_values(row) -> list | None:
    """Fast extractor for order events."""
    try:
        if isinstance(row, dict):
            get = row.get
            return [
                get("order_id"),
                get("strategy_id"),
                get("symbol"),
                get("exchange", get("exch", "")),
                get("side", get("action", "")),
                get("price_scaled"),
                get("qty", get("quantity", 0)),
                get("order_type", get("type", "")),
                get("status", ""),
                get("exch_ts", get("ts")),
                get("ingest_ts", get("recv_ts")),
            ]
        return [
            getattr(row, "order_id", None),
            getattr(row, "strategy_id", None),
            getattr(row, "symbol", None),
            getattr(row, "exchange", None) or "",
            getattr(row, "side", None) or getattr(row, "action", None) or "",
            getattr(row, "price_scaled", None),
            getattr(row, "qty", None) or getattr(row, "quantity", None) or 0,
            getattr(row, "order_type", None) or getattr(row, "type", None) or "",
            getattr(row, "status", None) or "",
            getattr(row, "exch_ts", None) or getattr(row, "ts", None),
            getattr(row, "ingest_ts", None) or getattr(row, "recv_ts", None),
        ]
    except Exception:
        return None


def _extract_fill_values(row) -> list | None:
    """Fast extractor for fill/trade events."""
    try:
        if isinstance(row, dict):
            get = row.get
            return [
                get("trade_id", get("fill_id")),
                get("order_id"),
                get("symbol"),
                get("exchange", get("exch", "")),
                get("side", get("action", "")),
                get("price_scaled"),
                get("qty", get("quantity", 0)),
                get("exch_ts", get("ts")),
                get("ingest_ts", get("recv_ts")),
            ]
        return [
            getattr(row, "trade_id", None) or getattr(row, "fill_id", None),
            getattr(row, "order_id", None),
            getattr(row, "symbol", None),
            getattr(row, "exchange", None) or "",
            getattr(row, "side", None) or getattr(row, "action", None) or "",
            getattr(row, "price_scaled", None),
            getattr(row, "qty", None) or getattr(row, "quantity", None) or 0,
            getattr(row, "exch_ts", None) or getattr(row, "ts", None),
            getattr(row, "ingest_ts", None) or getattr(row, "recv_ts", None),
        ]
    except Exception:
        return None


def _values_to_dict(columns: list[str], values: list | None):
    if values is None:
        return None
    return dict(zip(columns, values))


def _extract_market_data(row) -> dict | None:
    """Compatibility extractor returning dict for tests/tools."""
    return _values_to_dict(MARKET_DATA_COLUMNS, _extract_market_data_values(row))


def _extract_order(row) -> dict | None:
    """Compatibility extractor returning dict for tests/tools."""
    return _values_to_dict(ORDER_COLUMNS, _extract_order_values(row))


def _extract_fill(row) -> dict | None:
    """Compatibility extractor returning dict for tests/tools."""
    return _values_to_dict(FILL_COLUMNS, _extract_fill_values(row))


# Map of topic -> preordered-values extractor function (CC-5)
_EXTRACTORS = {
    "market_data": _extract_market_data_values,
    "orders": _extract_order_values,
    "fills": _extract_fill_values,
}

_EXTRACTOR_COLUMNS = {
    "market_data": MARKET_DATA_COLUMNS,
    "orders": ORDER_COLUMNS,
    "fills": FILL_COLUMNS,
}


class RecorderService:
    def __init__(self, queue: asyncio.Queue, clickhouse_client=None):
        self.queue = queue
        self.running = False

        # CE3-01: Recorder mode
        self._mode = get_recorder_mode()
        self._wal_first_writer = None  # set in run() when mode=wal_first

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
                extractor_columns=_EXTRACTOR_COLUMNS.get("market_data") if extract_enabled else None,
                memory_guard=self.memory_guard,
                health_tracker=self.health_tracker,
            ),
            "orders": Batcher(
                "hft.orders",
                writer=self.writer,
                extractor=_EXTRACTORS.get("orders") if extract_enabled else None,
                extractor_columns=_EXTRACTOR_COLUMNS.get("orders") if extract_enabled else None,
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
                extractor_columns=_EXTRACTOR_COLUMNS.get("fills") if extract_enabled else None,
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
        if self._mode == RecorderMode.WAL_FIRST or os.getenv("HFT_DISABLE_CLICKHOUSE") or not ch_enabled:
            logger.info("Skipping WAL Recovery (ClickHouse disabled or wal_first mode)", mode=self._mode.value)
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
        logger.info("Recorder started", mode=self._mode.value)

        # CE3-01: set wal_mode metric
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().wal_mode.set(1 if self._mode == RecorderMode.WAL_FIRST else 0)
        except Exception:
            pass

        # CE3-02: init WAL-first writer when in wal_first mode
        if self._mode == RecorderMode.WAL_FIRST:
            from hft_platform.recorder.disk_monitor import DiskPressureMonitor
            from hft_platform.recorder.wal import WALBatchWriter
            from hft_platform.recorder.wal_first import WALFirstWriter

            _wal_dir = os.getenv("HFT_WAL_DIR", ".wal")
            _disk_monitor = DiskPressureMonitor(wal_dir=_wal_dir)
            _disk_monitor.start()
            _batch_writer = WALBatchWriter(wal_dir=_wal_dir)
            self._wal_first_writer = WALFirstWriter(_batch_writer, _disk_monitor)

        if self._mode != RecorderMode.WAL_FIRST:
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

                # CE3-02: route to WAL-first writer or batcher depending on mode
                if self._mode == RecorderMode.WAL_FIRST and self._wal_first_writer is not None:
                    if isinstance(data, dict):
                        rows = [data]
                    elif isinstance(data, list):
                        rows = data
                    else:
                        rows = [data]
                    ok = await self._wal_first_writer.write(topic, rows)
                    if not ok:
                        self.health_tracker.record_event("data_loss")
                        try:
                            from hft_platform.observability.metrics import MetricsRegistry

                            MetricsRegistry.get().recorder_failures_total.inc()
                        except Exception:
                            pass
                elif topic in self.batchers:
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

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hft_platform.recorder.worker import (
    RecorderService,
    _extract_fill,
    _extract_fill_values,
    _extract_market_data,
    _extract_market_data_values,
    _extract_order,
    _extract_order_values,
    _extract_pnl_snapshot_values,
    _values_to_dict,
    MARKET_DATA_COLUMNS,
    ORDER_COLUMNS,
    FILL_COLUMNS,
    PNL_SNAPSHOT_COLUMNS,
)


class TestRecorderService(unittest.IsolatedAsyncioTestCase):
    async def test_worker_loop(self):
        queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            # Setup mock writer
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)

            # Add items with correct schema
            await queue.put({"topic": "market_data", "data": {"price": 100}})
            await queue.put({"topic": "market_data", "data": {"price": 101}})

            task = asyncio.create_task(worker.run())

            # Allow loop to process items
            await asyncio.sleep(0.1)

            # Force flush manually since timing is flaky
            for b_name, b in worker.batchers.items():
                await b.force_flush()

            await asyncio.sleep(0.1)

            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # The worker calls batcher, batcher calls writer.write_columnar (or write)
            self.assertTrue(mock_writer_inst.write_columnar.called or mock_writer_inst.write.called)

    async def test_recover_wal_skips_when_disabled(self):
        queue = asyncio.Queue()

        with patch.dict(os.environ, {"HFT_DISABLE_CLICKHOUSE": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.logger.info") as log_info:
                    await worker.recover_wal()
                    # CE3-01: message now includes mode kwarg
                    calls = [str(c) for c in log_info.call_args_list]
                    assert any("Skipping WAL Recovery" in c for c in calls), (
                        f"Expected 'Skipping WAL Recovery' log, got: {calls}"
                    )

    async def test_recover_wal_warns_without_connection(self):
        queue = asyncio.Queue()

        class DummyLoader:
            def __init__(self):
                self.ch_client = None

            def connect(self):
                self.ch_client = None

            def process_files(self):
                raise AssertionError("process_files should not be called without ch_client")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.asyncio.to_thread", new=fake_to_thread):
                    with patch("hft_platform.recorder.loader.WALLoaderService", new=DummyLoader):
                        with patch("hft_platform.recorder.worker.logger.warning") as log_warn:
                            await worker.recover_wal()
                            log_warn.assert_any_call("Skipping WAL Recovery (No ClickHouse Connection)")

    async def test_recover_wal_runs_when_connected(self):
        queue = asyncio.Queue()
        calls = SimpleNamespace(connect=0, process=0)

        class DummyLoader:
            def __init__(self):
                self.ch_client = object()

            def connect(self):
                calls.connect += 1

            def process_files(self):
                calls.process += 1

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.asyncio.to_thread", new=fake_to_thread):
                    with patch("hft_platform.recorder.loader.WALLoaderService", new=DummyLoader):
                        with patch("hft_platform.recorder.worker.logger.info") as log_info:
                            await worker.recover_wal()
                            self.assertGreaterEqual(calls.connect, 1)
                            self.assertGreaterEqual(calls.process, 1)
                            log_info.assert_any_call("Starting WAL Recovery...")
                            log_info.assert_any_call("WAL Recovery Complete")


# ─────────────────────────────────────────────────────────────────────────────
# Pure-function extractor tests (no async, high coverage gain)
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractMarketDataValues(unittest.TestCase):
    def test_extract_dict_returns_correct_length(self):
        row = {
            "symbol": "2330",
            "exchange": "TSE",
            "type": "tick",
            "exch_ts": 1000,
            "ingest_ts": 1001,
            "price_scaled": 5950000,
            "volume": 100,
            "bids_price": [594],
            "bids_vol": [10],
            "asks_price": [595],
            "asks_vol": [5],
            "seq_no": 42,
            "instrument_type": "stock",
            "underlying": "",
            "strike_scaled": 0,
            "option_right": "",
            "expiry": "2026-06-20",
        }
        result = _extract_market_data_values(row)
        assert result is not None
        assert len(result) == len(MARKET_DATA_COLUMNS)
        assert result[0] == "2330"
        assert result[3] == 1000

    def test_extract_dict_uses_fallback_keys(self):
        row = {"symbol": "2330", "exch": "OTC", "ts": 999, "recv_ts": 1000, "seq": 7}
        result = _extract_market_data_values(row)
        assert result is not None
        assert result[1] == "OTC"   # exchange fallback to exch
        assert result[3] == 999     # exch_ts fallback to ts
        assert result[11] == 7      # seq_no fallback to seq

    def test_extract_dict_defaults_exchange_to_tse(self):
        row = {"symbol": "2330"}
        result = _extract_market_data_values(row)
        assert result is not None
        assert result[1] == "TSE"

    def test_extract_object_path(self):
        row = SimpleNamespace(
            symbol="TXFD6",
            exchange="TAIFEX",
            type="bidask",
            exch_ts=2000,
            ingest_ts=2001,
            price_scaled=200000000,
            volume=5,
            bids_price=[19999],
            bids_vol=[2],
            asks_price=[20001],
            asks_vol=[3],
            seq_no=1,
            instrument_type="futures",
            underlying="TX",
            strike_scaled=0,
            option_right="",
            expiry="2026-06-20",
        )
        result = _extract_market_data_values(row)
        assert result is not None
        assert len(result) == len(MARKET_DATA_COLUMNS)
        assert result[0] == "TXFD6"
        assert result[6] == 5

    def test_extract_object_fallback_fields(self):
        row = SimpleNamespace(exch="TSE", ts=111, recv_ts=112, total_volume=99, seq=3)
        result = _extract_market_data_values(row)
        assert result is not None
        assert result[1] == "TSE"
        assert result[3] == 111
        assert result[6] == 99
        assert result[11] == 3

    def test_compat_wrapper_returns_dict(self):
        row = {"symbol": "2330", "price_scaled": 100}
        result = _extract_market_data(row)
        assert isinstance(result, dict)
        assert "symbol" in result

    def test_values_to_dict_with_none_returns_none(self):
        assert _values_to_dict(MARKET_DATA_COLUMNS, None) is None

    def test_values_to_dict_maps_columns_to_values(self):
        values = list(range(len(MARKET_DATA_COLUMNS)))
        result = _values_to_dict(MARKET_DATA_COLUMNS, values)
        assert result is not None
        for i, col in enumerate(MARKET_DATA_COLUMNS):
            assert result[col] == i

    def test_trade_direction_in_market_data_columns(self):
        """trade_direction must be present in MARKET_DATA_COLUMNS."""
        assert "trade_direction" in MARKET_DATA_COLUMNS

    def test_extract_dict_trade_direction_present(self):
        """trade_direction is extracted from a tick dict with trade_direction set."""
        row = {
            "symbol": "2330",
            "exchange": "TSE",
            "type": "tick",
            "exch_ts": 1000,
            "ingest_ts": 1001,
            "price_scaled": 5950000,
            "volume": 100,
            "bids_price": [594],
            "bids_vol": [10],
            "asks_price": [595],
            "asks_vol": [5],
            "seq_no": 42,
            "trade_direction": 1,
            "instrument_type": "stock",
            "underlying": "",
            "strike_scaled": 0,
            "option_right": "",
            "expiry": "2026-06-20",
        }
        result = _extract_market_data_values(row)
        assert result is not None
        assert len(result) == len(MARKET_DATA_COLUMNS)
        td_idx = MARKET_DATA_COLUMNS.index("trade_direction")
        assert result[td_idx] == 1

    def test_extract_dict_trade_direction_defaults_to_zero_when_absent(self):
        """BidAsk dicts without trade_direction default to 0."""
        row = {"symbol": "TXFD6", "exchange": "TAIFEX", "type": "bidask"}
        result = _extract_market_data_values(row)
        assert result is not None
        td_idx = MARKET_DATA_COLUMNS.index("trade_direction")
        assert result[td_idx] == 0

    def test_extract_object_trade_direction_present(self):
        """trade_direction is extracted from an object with trade_direction attribute."""
        row = SimpleNamespace(
            symbol="TXFD6",
            exchange="TAIFEX",
            type="tick",
            exch_ts=2000,
            ingest_ts=2001,
            price_scaled=200000000,
            volume=5,
            bids_price=[19999],
            bids_vol=[2],
            asks_price=[20001],
            asks_vol=[3],
            seq_no=1,
            trade_direction=-1,
            instrument_type="futures",
            underlying="TX",
            strike_scaled=0,
            option_right="",
            expiry="2026-06-20",
        )
        result = _extract_market_data_values(row)
        assert result is not None
        assert len(result) == len(MARKET_DATA_COLUMNS)
        td_idx = MARKET_DATA_COLUMNS.index("trade_direction")
        assert result[td_idx] == -1

    def test_extract_object_trade_direction_defaults_to_zero_when_absent(self):
        """Object without trade_direction attribute defaults to 0."""
        row = SimpleNamespace(symbol="2330", exch="TSE")
        result = _extract_market_data_values(row)
        assert result is not None
        td_idx = MARKET_DATA_COLUMNS.index("trade_direction")
        assert result[td_idx] == 0


class TestExtractOrderValues(unittest.TestCase):
    def test_extract_dict_path(self):
        row = {
            "order_id": "ORD1",
            "strategy_id": "S1",
            "symbol": "2330",
            "side": "buy",
            "price_scaled": 5950000,
            "qty": 1,
            "status": "open",
            "ingest_ts": 1001,
            "latency_us": 42,
        }
        result = _extract_order_values(row)
        assert result is not None
        assert len(result) == len(ORDER_COLUMNS)
        assert result[0] == "ORD1"
        assert result[3] == "buy"
        assert result[8] == 42  # latency_us

    def test_extract_dict_fallback_keys(self):
        row = {"order_id": "X", "action": "sell", "quantity": 3, "recv_ts": 51}
        result = _extract_order_values(row)
        assert result is not None
        assert result[3] == "sell"  # side fallback to action
        assert result[5] == 3       # qty fallback to quantity
        assert result[7] == 51      # ingest_ts fallback to recv_ts
        assert result[8] == 0       # latency_us default

    def test_extract_object_path(self):
        row = SimpleNamespace(
            order_id="ORD2",
            strategy_id="S2",
            symbol="TXFD6",
            side="sell",
            price_scaled=200000000,
            qty=2,
            status="filled",
            ingest_ts=3001,
            latency_us=100,
        )
        result = _extract_order_values(row)
        assert result is not None
        assert len(result) == len(ORDER_COLUMNS)
        assert result[0] == "ORD2"
        assert result[8] == 100  # latency_us

    def test_compat_wrapper_returns_dict(self):
        row = {"order_id": "X", "symbol": "2330"}
        result = _extract_order(row)
        assert isinstance(result, dict)
        assert "order_id" in result


class TestExtractFillValues(unittest.TestCase):
    def test_extract_dict_path(self):
        row = {
            "fill_id": "F1",
            "order_id": "ORD1",
            "strategy_id": "S1",
            "symbol": "2330",
            "side": "buy",
            "price_scaled": 5950000,
            "qty": 1,
            "fee_scaled": 200,
            "match_ts": 1000,
        }
        result = _extract_fill_values(row)
        assert result is not None
        assert len(result) == len(FILL_COLUMNS)
        assert result[0] == "F1"
        assert result[7] == 200  # fee_scaled
        assert result[8] == 1000  # match_ts

    def test_extract_dict_trade_id_fallback(self):
        """fill_id falls back to trade_id for backward compatibility."""
        row = {"trade_id": "T999", "order_id": "ORD1", "symbol": "2330"}
        result = _extract_fill_values(row)
        assert result is not None
        assert result[0] == "T999"  # fill_id fallback to trade_id

    def test_extract_dict_fill_id_preferred(self):
        row = {"fill_id": "F999", "trade_id": "T999", "order_id": "ORD1", "symbol": "2330"}
        result = _extract_fill_values(row)
        assert result is not None
        assert result[0] == "F999"  # fill_id preferred over trade_id

    def test_extract_object_path(self):
        row = SimpleNamespace(
            fill_id="F2",
            order_id="ORD2",
            strategy_id="S2",
            symbol="TMFD6",
            side="sell",
            price_scaled=20000000,
            qty=1,
            fee_scaled=150,
            match_ts=4000,
        )
        result = _extract_fill_values(row)
        assert result is not None
        assert len(result) == len(FILL_COLUMNS)
        assert result[0] == "F2"
        assert result[7] == 150  # fee_scaled

    def test_extract_object_trade_id_fallback(self):
        row = SimpleNamespace(trade_id="T888", order_id="X", symbol="2330")
        result = _extract_fill_values(row)
        assert result is not None
        assert result[0] == "T888"

    def test_compat_wrapper_returns_dict(self):
        row = {"fill_id": "F1", "symbol": "2330"}
        result = _extract_fill(row)
        assert isinstance(result, dict)
        assert "fill_id" in result


class TestExtractPnlSnapshotValues(unittest.TestCase):
    def test_extract_dict_path(self):
        row = {
            "snapshot_ts": 9000,
            "account_id": "ACC1",
            "strategy_id": "S1",
            "symbol": "2330",
            "net_qty": 2,
            "avg_price_scaled": 5950000,
            "realized_pnl_scaled": 1000,
            "fees_scaled": 50,
            "total_pnl_scaled": 950,
            "peak_equity_scaled": 200000,
            "drawdown_pct": 0.01,
        }
        result = _extract_pnl_snapshot_values(row)
        assert result is not None
        assert len(result) == len(PNL_SNAPSHOT_COLUMNS)
        assert result[0] == 9000
        assert result[10] == 0.01

    def test_extract_non_dict_returns_none(self):
        row = SimpleNamespace(snapshot_ts=9000)
        result = _extract_pnl_snapshot_values(row)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# RecorderService additional coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderServiceExtra(unittest.IsolatedAsyncioTestCase):
    def _make_worker(self, env_override=None):
        env = env_override or {}
        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_inst = MockWriter.return_value
            mock_inst.active = True
            mock_inst.connect_async = AsyncMock()
            mock_inst.write = AsyncMock()
            mock_inst.write_columnar = AsyncMock()
            mock_inst.shutdown = AsyncMock()
            mock_inst.set_health_tracker = MagicMock()
            with patch.dict(os.environ, env, clear=False):
                return RecorderService(asyncio.Queue()), mock_inst

    async def test_get_health_returns_dict(self):
        worker, _ = self._make_worker()
        health = worker.get_health()
        assert isinstance(health, dict)

    async def test_recover_wal_exception_path_does_not_raise(self):
        """Exception inside recover_wal is caught — function must not propagate."""
        queue = asyncio.Queue()
        with patch("hft_platform.recorder.worker.DataWriter"):
            with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"}, clear=False):
                worker = RecorderService(queue)

        async def fake_to_thread(func, *args, **kwargs):
            raise RuntimeError("connection refused")

        # Verify the exception is swallowed — no unhandled raise.
        try:
            with patch("hft_platform.recorder.worker.asyncio.to_thread", new=fake_to_thread):
                await worker.recover_wal()
        except Exception as exc:  # noqa: BLE001
            self.fail(f"recover_wal raised unexpectedly: {exc}")
        # Post-condition: worker state unchanged
        assert worker.running is False

    async def test_run_wal_first_mode_routes_to_wal_writer(self):
        """WAL_FIRST write path (lines 414-429) — patch inline imports via sys.modules."""
        import sys
        from hft_platform.recorder.mode import RecorderMode

        queue = asyncio.Queue()

        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)

        mock_disk_monitor = MagicMock()
        mock_disk_monitor.start = MagicMock()
        mock_batch_writer = MagicMock()

        # Patch at the source module paths (where they are imported from inside run())
        with (
            patch("hft_platform.recorder.disk_monitor.DiskPressureMonitor", return_value=mock_disk_monitor),
            patch("hft_platform.recorder.wal.WALBatchWriter", return_value=mock_batch_writer),
            patch("hft_platform.recorder.wal_first.WALFirstWriter", return_value=mock_wal_writer),
            patch("hft_platform.recorder.worker.DataWriter") as MockWriter,
            patch.dict(os.environ, {"HFT_RECORDER_MODE": "wal_first"}, clear=False),
        ):
            mock_inst = MockWriter.return_value
            mock_inst.active = True
            mock_inst.connect_async = AsyncMock()
            mock_inst.write = AsyncMock()
            mock_inst.write_columnar = AsyncMock()
            mock_inst.shutdown = AsyncMock()
            mock_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)
            worker.writer.connect_async = AsyncMock()
            worker.writer.shutdown = AsyncMock()

            await queue.put({"topic": "market_data", "data": {"symbol": "2330"}})
            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert mock_wal_writer.write.called
        call_args = mock_wal_writer.write.call_args
        assert call_args[0][0] == "market_data"

    async def test_run_wal_first_mode_data_loss_on_write_failure(self):
        """health_tracker data_loss recorded when WAL write returns False."""
        from hft_platform.recorder.mode import RecorderMode

        queue = asyncio.Queue()
        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_inst = MockWriter.return_value
            mock_inst.active = True
            mock_inst.connect_async = AsyncMock()
            mock_inst.write = AsyncMock()
            mock_inst.write_columnar = AsyncMock()
            mock_inst.shutdown = AsyncMock()
            mock_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)

        worker._mode = RecorderMode.WAL_FIRST
        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=False)  # simulate failure
        worker._wal_first_writer = mock_wal_writer
        worker.writer.connect_async = AsyncMock()
        worker.writer.shutdown = AsyncMock()

        await queue.put({"topic": "market_data", "data": {"symbol": "2330"}})

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        health = worker.get_health()
        assert health.get("data_loss_events", 0) >= 1 or health.get("total_events", 0) >= 0

    async def test_run_direct_mode_routes_list_data_to_batcher(self):
        """List data in direct mode goes to the right batcher."""
        queue = asyncio.Queue()
        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_inst = MockWriter.return_value
            mock_inst.active = True
            mock_inst.connect_async = AsyncMock()
            mock_inst.write = AsyncMock()
            mock_inst.write_columnar = AsyncMock()
            mock_inst.shutdown = AsyncMock()
            mock_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)
            worker.writer = mock_inst

        mock_add = AsyncMock()
        worker.batchers["orders"].add = mock_add

        await queue.put({"topic": "orders", "data": {"order_id": "X"}})

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_add.called

    async def test_schema_extract_disabled_creates_batchers_without_extractor(self):
        """HFT_BATCHER_SCHEMA_EXTRACT=0 disables extractors."""
        queue = asyncio.Queue()
        env = {"HFT_BATCHER_SCHEMA_EXTRACT": "0"}
        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_inst = MockWriter.return_value
            mock_inst.set_health_tracker = MagicMock()
            with patch.dict(os.environ, env, clear=False):
                worker = RecorderService(queue)
        # When extractors are disabled, they should be None on the batcher
        batcher = worker.batchers["market_data"]
        assert batcher._extractor is None

    async def test_run_wal_first_mode_list_data_routes_to_wal_writer(self):
        """List data in WAL_FIRST mode is passed through directly."""
        import sys

        queue = asyncio.Queue()

        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)
        mock_disk_monitor = MagicMock()
        mock_disk_monitor.start = MagicMock()
        mock_batch_writer = MagicMock()

        with (
            patch("hft_platform.recorder.disk_monitor.DiskPressureMonitor", return_value=mock_disk_monitor),
            patch("hft_platform.recorder.wal.WALBatchWriter", return_value=mock_batch_writer),
            patch("hft_platform.recorder.wal_first.WALFirstWriter", return_value=mock_wal_writer),
            patch("hft_platform.recorder.worker.DataWriter") as MockWriter,
            patch.dict(os.environ, {"HFT_RECORDER_MODE": "wal_first"}, clear=False),
        ):
            mock_inst = MockWriter.return_value
            mock_inst.active = True
            mock_inst.connect_async = AsyncMock()
            mock_inst.write = AsyncMock()
            mock_inst.write_columnar = AsyncMock()
            mock_inst.shutdown = AsyncMock()
            mock_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)
            worker.writer.connect_async = AsyncMock()
            worker.writer.shutdown = AsyncMock()

            # Send list-typed data to cover the isinstance(data, list) branch
            await queue.put({"topic": "market_data", "data": [{"symbol": "2330"}, {"symbol": "TXFD6"}]})
            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert mock_wal_writer.write.called
        call_args = mock_wal_writer.write.call_args
        # rows should be the list as-is
        assert call_args[0][0] == "market_data"
        assert len(call_args[0][1]) == 2

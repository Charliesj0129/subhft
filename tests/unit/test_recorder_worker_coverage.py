"""Tests for recorder/worker.py: service lifecycle, routing, drain, shutdown paths.

Covers missing lines in recorder/worker.py:
  - _to_date string/date conversion (lines 22, 26-27)
  - _extract_market_data_values exception path (lines 151-153)
  - _extract_order_values exception path (lines 187-189)
  - _extract_fill_values object path fallbacks (lines 251-252)
  - _getattr_scaled fallback to _scaled suffix (lines 274-275)
  - RecorderService.run: wal_first non-dict/non-list data path (line 497)
  - RecorderService.run: wal_first write failure metric path (lines 502-509)
  - RecorderService.run: unknown topic drops (lines 514-519)
  - RecorderService.run: exception in processing loop (lines 526, 536-537)
  - RecorderService.run: shutdown flush timeout/cancelled (lines 563-564)
  - RecorderService._drain_queue_into_batchers (lines 575-601)
  - RecorderService._shutdown_flush: CancelledError, WAL flush, writer error (line 644)
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.worker import (
    MARKET_DATA_COLUMNS,
    RecorderService,
    _extract_fill,
    _extract_fill_values,
    _extract_market_data,
    _extract_market_data_values,
    _extract_order,
    _extract_order_values,
    _extract_pnl_snapshot_values,
    _getattr_scaled,
    _to_date,
    _values_to_dict,
)

# -- Shared helpers -----------------------------------------------------------


def _make_worker(env_override: dict | None = None):
    """Create a RecorderService with mocked DataWriter. Returns (worker, mock_writer)."""
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
            queue = asyncio.Queue()
            return RecorderService(queue), mock_inst


# -- _to_date conversion -----------------------------------------------------


class TestToDate:
    def test_returns_date_when_date_instance(self) -> None:
        d = date(2026, 4, 16)
        assert _to_date(d) == d

    def test_parses_valid_iso_string(self) -> None:
        result = _to_date("2026-06-20")
        assert result == date(2026, 6, 20)

    def test_returns_epoch_for_epoch_string(self) -> None:
        result = _to_date("1970-01-01")
        assert result == date(1970, 1, 1)

    def test_returns_epoch_for_none(self) -> None:
        result = _to_date(None)
        assert result == date(1970, 1, 1)

    def test_returns_epoch_for_empty_string(self) -> None:
        result = _to_date("")
        assert result == date(1970, 1, 1)

    def test_returns_epoch_for_invalid_date_string(self) -> None:
        result = _to_date("not-a-date")
        assert result == date(1970, 1, 1)


# -- Extractor: market data --------------------------------------------------


class TestExtractMarketData:
    def test_extract_from_dict(self) -> None:
        row = {
            "symbol": "TXFD6",
            "exch": "TAIFEX",
            "type": "tick",
            "ts": 1000,
            "recv_ts": 2000,
            "price_scaled": 200000000,
            "total_volume": 10,
            "seq": 5,
        }
        values = _extract_market_data_values(row)
        assert values is not None
        assert values[0] == "TXFD6"
        assert values[1] == "TAIFEX"

    def test_extract_from_object(self) -> None:
        obj = SimpleNamespace(
            symbol="2330",
            exchange="TSE",
            type="tick",
            exch_ts=1000,
            ingest_ts=2000,
            price_scaled=100000,
            volume=5,
            bids_price=None,
            bids_vol=None,
            asks_price=None,
            asks_vol=None,
            seq_no=1,
            trade_direction=0,
            instrument_type="",
            underlying="",
            strike_scaled=0,
            option_right="",
            expiry="1970-01-01",
        )
        values = _extract_market_data_values(obj)
        assert values is not None
        assert values[0] == "2330"

    def test_extract_returns_none_on_exception(self) -> None:
        class BrokenObj:
            def __getattr__(self, name):
                raise TypeError("broken")

        result = _extract_market_data_values(BrokenObj())
        assert result is None

    def test_extract_dict_returns_full_dict(self) -> None:
        row = {"symbol": "TEST", "price_scaled": 100}
        result = _extract_market_data(row)
        assert result is not None
        assert result["symbol"] == "TEST"
        assert set(MARKET_DATA_COLUMNS).issubset(result.keys())


# -- Extractor: orders --------------------------------------------------------


class TestExtractOrder:
    def test_extract_from_dict(self) -> None:
        row = {
            "order_id": "O1",
            "strategy_id": "S1",
            "symbol": "TXFD6",
            "side": "BUY",
            "price_scaled": 200000000,
            "qty": 1,
            "status": "filled",
        }
        values = _extract_order_values(row)
        assert values is not None
        assert values[0] == "O1"

    def test_extract_from_dict_with_action_fallback(self) -> None:
        row = {"order_id": "O2", "action": "SELL", "quantity": 5}
        values = _extract_order_values(row)
        assert values is not None
        assert values[3] == "SELL"
        assert values[5] == 5

    def test_extract_from_object(self) -> None:
        obj = SimpleNamespace(
            order_id="O3",
            strategy_id="S1",
            symbol="TEST",
            action="BUY",
            price_scaled=100,
            quantity=2,
            status="new",
            ingest_ts=999,
            latency_us=10,
            instrument_type="",
            oc_type="",
        )
        values = _extract_order_values(obj)
        assert values is not None
        assert values[0] == "O3"

    def test_extract_returns_none_on_exception(self) -> None:
        class BrokenObj:
            def __getattr__(self, name):
                raise TypeError("broken")

        result = _extract_order_values(BrokenObj())
        assert result is None

    def test_extract_dict_returns_full_dict(self) -> None:
        row = {"order_id": "O1"}
        result = _extract_order(row)
        assert result is not None
        assert result["order_id"] == "O1"


# -- Extractor: fills ---------------------------------------------------------


class TestExtractFill:
    def test_extract_from_dict(self) -> None:
        row = {
            "ts_exchange": 1000,
            "ts_local": 2000,
            "fill_id": "F1",
            "symbol": "TXFD6",
            "side": "BUY",
            "qty": 1,
            "price_scaled": 200000000,
        }
        values = _extract_fill_values(row)
        assert values is not None
        assert values[0] == 1000

    def test_extract_from_dict_with_fallbacks(self) -> None:
        row = {
            "match_ts": 3000,
            "ingest_ts": 4000,
            "order_id": "O1",
            "trade_id": "T1",
            "action": "SELL",
            "quantity": 5,
            "price": 100,
        }
        values = _extract_fill_values(row)
        assert values is not None
        assert values[0] == 3000

    def test_extract_from_object_with_getattr_scaled(self) -> None:
        obj = SimpleNamespace(
            ts_exchange=1000,
            ts_local=2000,
            client_order_id="CO1",
            broker_order_id="BO1",
            fill_id="F1",
            strategy_id="S1",
            symbol="TEST",
            side="BUY",
            qty=1,
            price=200000000,
            fee=100,
            tax=50,
            decision_price=0,
            arrival_price=0,
            source="live",
            instrument_type="",
            oc_type="",
        )
        values = _extract_fill_values(obj)
        assert values is not None
        assert values[9] == 200000000  # price via _getattr_scaled

    def test_extract_from_object_with_scaled_suffix(self) -> None:
        """Test _getattr_scaled fallback to <field>_scaled."""
        obj = SimpleNamespace(
            ts_exchange=1000,
            ts_local=2000,
            client_order_id="CO1",
            broker_order_id="BO1",
            fill_id="F1",
            strategy_id="S1",
            symbol="TEST",
            side="BUY",
            qty=1,
            price_scaled=200000000,
            fee_scaled=100,
            tax_scaled=50,
            decision_price=0,
            arrival_price=0,
            source="live",
            instrument_type="",
            oc_type="",
        )
        values = _extract_fill_values(obj)
        assert values is not None
        assert values[9] == 200000000

    def test_extract_returns_none_on_exception(self) -> None:
        class BrokenObj:
            def __getattr__(self, name):
                raise TypeError("broken")

        result = _extract_fill_values(BrokenObj())
        assert result is None

    def test_extract_dict_returns_full_dict(self) -> None:
        row = {"fill_id": "F1", "price_scaled": 100}
        result = _extract_fill(row)
        assert result is not None
        assert result["fill_id"] == "F1"


# -- _getattr_scaled fallback -------------------------------------------------


class TestGetAttrScaled:
    def test_returns_plain_field_when_present(self) -> None:
        obj = SimpleNamespace(price=5000)
        assert _getattr_scaled(obj, "price") == 5000

    def test_falls_back_to_scaled_suffix(self) -> None:
        obj = SimpleNamespace(price_scaled=5000)
        assert _getattr_scaled(obj, "price") == 5000

    def test_returns_none_when_neither_present(self) -> None:
        obj = SimpleNamespace()
        assert _getattr_scaled(obj, "price") is None

    def test_returns_zero_value_not_none(self) -> None:
        """_getattr_scaled returns 0 (not None) when plain field is 0."""
        obj = SimpleNamespace(fee=0)
        assert _getattr_scaled(obj, "fee") == 0


# -- _extract_pnl_snapshot_values ---------------------------------------------


class TestExtractPnlSnapshot:
    def test_extract_from_dict(self) -> None:
        row = {
            "snapshot_ts": 1000,
            "account_id": "acc1",
            "strategy_id": "s1",
            "symbol": "TXFD6",
            "net_qty": 5,
            "avg_price_scaled": 200000000,
            "realized_pnl_scaled": 50000,
            "fees_scaled": 100,
            "total_pnl_scaled": 49900,
            "peak_equity_scaled": 1000000000,
            "drawdown_pct": 0.01,
        }
        values = _extract_pnl_snapshot_values(row)
        assert values is not None
        assert values[0] == 1000

    def test_extract_from_non_dict_returns_none(self) -> None:
        obj = SimpleNamespace(snapshot_ts=1000)
        result = _extract_pnl_snapshot_values(obj)
        assert result is None


# -- _values_to_dict ----------------------------------------------------------


class TestValuesToDict:
    def test_returns_dict_from_values(self) -> None:
        result = _values_to_dict(["a", "b"], [1, 2])
        assert result == {"a": 1, "b": 2}

    def test_returns_none_for_none_values(self) -> None:
        result = _values_to_dict(["a"], None)
        assert result is None


# -- RecorderService: unknown topic drops -------------------------------------


class TestUnknownTopicDrop:
    @pytest.mark.asyncio
    async def test_unknown_topic_increments_counter(self) -> None:
        worker, mock_writer = _make_worker()

        await worker.queue.put({"topic": "nonexistent_topic", "data": {"x": 1}})

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert worker._unknown_topic_drops >= 1

    @pytest.mark.asyncio
    async def test_invalid_item_no_topic_skipped(self) -> None:
        worker, mock_writer = _make_worker()

        await worker.queue.put({"data": {"x": 1}})

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert worker._unknown_topic_drops == 0


# -- RecorderService: WAL-first data routing ----------------------------------


class TestWalFirstDataRouting:
    @pytest.mark.asyncio
    async def test_wal_first_non_dict_non_list_wrapped(self) -> None:
        queue = asyncio.Queue()
        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)
        mock_wal_writer.flush = AsyncMock()
        mock_disk_monitor = MagicMock()
        mock_disk_monitor.start = MagicMock()
        mock_batch_writer = MagicMock()

        ns_data = SimpleNamespace(symbol="2330", price=100)

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

            await queue.put({"topic": "market_data", "data": ns_data})
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
        assert call_args[0][1] == [ns_data]

    @pytest.mark.asyncio
    async def test_wal_first_dict_data_wrapped_in_list(self) -> None:
        """Dict data in WAL_FIRST mode is wrapped as [data]."""
        queue = asyncio.Queue()
        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)
        mock_wal_writer.flush = AsyncMock()

        with (
            patch("hft_platform.recorder.disk_monitor.DiskPressureMonitor", return_value=MagicMock()),
            patch("hft_platform.recorder.wal.WALBatchWriter", return_value=MagicMock()),
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

            await queue.put({"topic": "orders", "data": {"order_id": "O1"}})
            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        call_args = mock_wal_writer.write.call_args
        assert call_args[0][1] == [{"order_id": "O1"}]


# -- RecorderService: processing exception path -------------------------------


class TestProcessingException:
    @pytest.mark.asyncio
    async def test_processing_exception_increments_error_counter(self) -> None:
        worker, mock_writer = _make_worker()
        mock_add = AsyncMock(side_effect=RuntimeError("batcher failed"))
        worker.batchers["orders"].add = mock_add

        await worker.queue.put({"topic": "orders", "data": {"order_id": "O1"}})

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert worker._process_errors >= 1
        assert worker.healthy is False


# -- RecorderService._drain_queue_into_batchers --------------------------------


class TestDrainQueue:
    @pytest.mark.asyncio
    async def test_drain_routes_items_to_batchers(self) -> None:
        worker, mock_writer = _make_worker()
        mock_add = AsyncMock()
        worker.batchers["market_data"].add = mock_add

        await worker.queue.put({"topic": "market_data", "data": {"sym": "2330"}})
        await worker.queue.put({"topic": "market_data", "data": {"sym": "TXFD6"}})

        drained = await worker._drain_queue_into_batchers()
        assert drained == 2
        assert mock_add.call_count == 2

    @pytest.mark.asyncio
    async def test_drain_skips_items_without_topic(self) -> None:
        worker, mock_writer = _make_worker()
        await worker.queue.put({"data": {"sym": "2330"}})
        await worker.queue.put({"topic": "market_data", "data": {"sym": "TXFD6"}})

        mock_add = AsyncMock()
        worker.batchers["market_data"].add = mock_add

        drained = await worker._drain_queue_into_batchers()
        assert drained == 1

    @pytest.mark.asyncio
    async def test_drain_handles_exception_in_processing(self) -> None:
        worker, mock_writer = _make_worker()
        mock_add = AsyncMock(side_effect=RuntimeError("drain error"))
        worker.batchers["market_data"].add = mock_add

        await worker.queue.put({"topic": "market_data", "data": {"sym": "2330"}})
        drained = await worker._drain_queue_into_batchers()
        assert drained == 0
        assert worker.queue.empty()

    @pytest.mark.asyncio
    async def test_drain_empty_queue_returns_zero(self) -> None:
        worker, mock_writer = _make_worker()
        drained = await worker._drain_queue_into_batchers()
        assert drained == 0

    @pytest.mark.asyncio
    async def test_drain_wal_first_mode_routes_to_wal_writer(self) -> None:
        from hft_platform.recorder.mode import RecorderMode

        worker, mock_writer = _make_worker()
        worker._mode = RecorderMode.WAL_FIRST

        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)
        worker._wal_first_writer = mock_wal_writer

        await worker.queue.put({"topic": "orders", "data": {"order_id": "O1"}})
        drained = await worker._drain_queue_into_batchers()
        assert drained == 1
        assert mock_wal_writer.write.called

    @pytest.mark.asyncio
    async def test_drain_wal_first_list_data_not_wrapped(self) -> None:
        """In WAL_FIRST drain, list data is passed through as-is."""
        from hft_platform.recorder.mode import RecorderMode

        worker, mock_writer = _make_worker()
        worker._mode = RecorderMode.WAL_FIRST

        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=True)
        worker._wal_first_writer = mock_wal_writer

        data = [{"id": "A"}, {"id": "B"}]
        await worker.queue.put({"topic": "fills", "data": data})
        drained = await worker._drain_queue_into_batchers()
        assert drained == 1
        call_args = mock_wal_writer.write.call_args
        assert call_args[0][1] == data


# -- RecorderService._shutdown_flush -------------------------------------------


class TestShutdownFlush:
    @pytest.mark.asyncio
    async def test_shutdown_flush_handles_batcher_exception(self) -> None:
        worker, mock_writer = _make_worker()
        worker.batchers["market_data"].force_flush = AsyncMock(side_effect=RuntimeError("flush err"))
        worker.batchers["orders"].force_flush = AsyncMock()

        await worker._shutdown_flush()
        assert worker.batchers["orders"].force_flush.called

    @pytest.mark.asyncio
    async def test_shutdown_flush_handles_writer_shutdown_error(self) -> None:
        worker, mock_writer = _make_worker()
        worker.writer.shutdown = AsyncMock(side_effect=RuntimeError("shutdown boom"))

        for batcher in worker.batchers.values():
            batcher.force_flush = AsyncMock()

        await worker._shutdown_flush()
        assert worker.writer.shutdown.called

    @pytest.mark.asyncio
    async def test_shutdown_flush_flushes_wal_first_writer(self) -> None:
        worker, mock_writer = _make_worker()
        mock_wfw = MagicMock()
        mock_wfw.flush = AsyncMock()
        worker._wal_first_writer = mock_wfw

        for batcher in worker.batchers.values():
            batcher.force_flush = AsyncMock()

        await worker._shutdown_flush()
        assert mock_wfw.flush.called

    @pytest.mark.asyncio
    async def test_shutdown_flush_catches_wal_first_writer_error(self) -> None:
        worker, mock_writer = _make_worker()
        mock_wfw = MagicMock()
        mock_wfw.flush = AsyncMock(side_effect=RuntimeError("wal flush boom"))
        worker._wal_first_writer = mock_wfw

        for batcher in worker.batchers.values():
            batcher.force_flush = AsyncMock()

        await worker._shutdown_flush()
        assert mock_wfw.flush.called

    @pytest.mark.asyncio
    async def test_shutdown_flush_cancel_records_skipped_batchers(self) -> None:
        worker, mock_writer = _make_worker()
        call_count = 0

        async def conditional_flush():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise asyncio.CancelledError()

        for batcher in worker.batchers.values():
            batcher.force_flush = AsyncMock(side_effect=conditional_flush)

        with pytest.raises(asyncio.CancelledError):
            await worker._shutdown_flush()

        assert call_count >= 2


# -- RecorderService: WAL-first write failure ----------------------------------


class TestWalFirstWriteFailure:
    @pytest.mark.asyncio
    async def test_wal_first_write_failure_records_data_loss(self) -> None:
        queue = asyncio.Queue()
        mock_wal_writer = MagicMock()
        mock_wal_writer.write = AsyncMock(return_value=False)
        mock_wal_writer.flush = AsyncMock()

        with (
            patch("hft_platform.recorder.disk_monitor.DiskPressureMonitor", return_value=MagicMock()),
            patch("hft_platform.recorder.wal.WALBatchWriter", return_value=MagicMock()),
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

            await queue.put({"topic": "market_data", "data": {"sym": "2330"}})
            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert worker.healthy is False


# -- RecorderService: get_health -----------------------------------------------


class TestGetHealth:
    def test_get_health_returns_dict(self) -> None:
        worker, _ = _make_worker()
        health = worker.get_health()
        assert isinstance(health, dict)


# -- RecorderService: normal topic routing ------------------------------------


class TestNormalTopicRouting:
    @pytest.mark.asyncio
    async def test_routes_known_topic_to_batcher(self) -> None:
        worker, mock_writer = _make_worker()
        mock_add = AsyncMock()
        worker.batchers["market_data"].add = mock_add

        await worker.queue.put({"topic": "market_data", "data": {"sym": "2330"}})
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_add.called
        assert worker.last_write_ok > 0

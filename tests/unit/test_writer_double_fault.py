"""Tests for writer double-fault raise and batcher reinject health tracker wiring.

Covers:
- Bug A: write_columnar / write raise WriterDoubleFaultError on CH+WAL double-fault
- Bug B: reinject circuit breaker and reinject failure notify health_tracker
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_writer(monkeypatch, *, wal_ok: bool = True, ch_ok: bool = False, health_tracker=None):
    """Build a DataWriter with controlled CH/WAL behavior."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.delenv("HFT_CLICKHOUSE_HOST", raising=False)
    monkeypatch.delenv("HFT_CLICKHOUSE_PORT", raising=False)

    with (
        patch("hft_platform.observability.metrics.MetricsRegistry") as mock_mr,
        patch("hft_platform.recorder.writer.WALWriter") as mock_wal_cls,
    ):
        mock_mr.get.return_value = None

        from hft_platform.recorder.writer import DataWriter

        writer = DataWriter(ch_host="localhost", ch_port=9000)
        writer.metrics = None
        writer.connected = ch_ok
        writer.ch_client = MagicMock() if ch_ok else None

        # Mock WAL to return wal_ok
        mock_wal = AsyncMock()
        mock_wal.write = AsyncMock(return_value=wal_ok)
        writer.wal = mock_wal

        # Disable batch writer so we hit the simple WAL path
        writer._get_wal_batch_writer = MagicMock(return_value=None)

        if health_tracker is not None:
            writer._health_tracker = health_tracker

        return writer


def _make_batcher(monkeypatch, *, health_tracker=None, reinject_max=3):
    """Build a Batcher with mock writer and configurable health tracker."""
    monkeypatch.setenv("HFT_BATCHER_REINJECT_MAX", str(reinject_max))

    with patch("hft_platform.observability.metrics.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = MagicMock()

        from hft_platform.recorder.batcher import Batcher

        batcher = Batcher(
            table_name="hft.test_table",
            writer=MagicMock(),
            health_tracker=health_tracker,
        )
        return batcher


def _make_flush_buf():
    """Create a ColumnarBuffer with some test rows."""
    from hft_platform.recorder.batcher import ColumnarBuffer

    buf = ColumnarBuffer(table_name="hft.test_table")
    buf.append_row({"col_a": 1, "col_b": 2})
    buf.append_row({"col_a": 3, "col_b": 4})
    return buf


# ===================================================================
# Bug A: WriterDoubleFaultError on CH + WAL failure
# ===================================================================


class TestWriterDoubleFaultRaise:
    """write_columnar and write must raise WriterDoubleFaultError on double-fault."""

    @pytest.mark.asyncio
    async def test_write_columnar_raises_on_double_fault(self, monkeypatch):
        """When both CH and WAL fail, write_columnar raises WriterDoubleFaultError."""
        from hft_platform.recorder.writer import WriterDoubleFaultError

        ht = MagicMock()
        writer = _make_writer(monkeypatch, ch_ok=False, wal_ok=False, health_tracker=ht)

        with pytest.raises(WriterDoubleFaultError, match="Both CH and WAL failed"):
            await writer.write_columnar(
                table="hft.test_table",
                column_names=["col_a", "col_b"],
                column_data=[[1, 3], [2, 4]],
                row_count=2,
            )

        ht.record_event.assert_called_with("data_loss", table="hft.test_table", count=2)

    @pytest.mark.asyncio
    async def test_write_columnar_no_raise_when_wal_succeeds(self, monkeypatch):
        """When CH fails but WAL succeeds, write_columnar returns normally."""

        writer = _make_writer(monkeypatch, ch_ok=False, wal_ok=True)

        # Should not raise
        await writer.write_columnar(
            table="hft.test_table",
            column_names=["col_a", "col_b"],
            column_data=[[1, 3], [2, 4]],
            row_count=2,
        )

    @pytest.mark.asyncio
    async def test_write_raises_on_double_fault(self, monkeypatch):
        """Legacy write() also raises WriterDoubleFaultError on double-fault."""
        from hft_platform.recorder.writer import WriterDoubleFaultError

        ht = MagicMock()
        writer = _make_writer(monkeypatch, ch_ok=False, wal_ok=False, health_tracker=ht)

        with pytest.raises(WriterDoubleFaultError, match="Both CH and WAL failed"):
            await writer.write("hft.test_table", [{"col_a": 1, "col_b": 2}])

        ht.record_event.assert_called_with("data_loss", table="hft.test_table", count=1)

    @pytest.mark.asyncio
    async def test_write_no_raise_when_wal_succeeds(self, monkeypatch):
        """Legacy write() returns normally when WAL succeeds after CH failure."""
        writer = _make_writer(monkeypatch, ch_ok=False, wal_ok=True)

        await writer.write("hft.test_table", [{"col_a": 1, "col_b": 2}])


# ===================================================================
# Bug B: Reinject circuit breaker notifies health tracker
# ===================================================================


class TestReinjectHealthTracker:
    """Reinject circuit breaker and reinject failure must call health_tracker."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_calls_health_tracker(self, monkeypatch):
        """When reinject circuit breaker trips, health_tracker.record_event is called."""
        ht = MagicMock()
        batcher = _make_batcher(monkeypatch, health_tracker=ht, reinject_max=1)
        flush_buf = _make_flush_buf()

        # Exhaust the circuit breaker (max=1, so 2nd call trips it)
        batcher._reinject_consecutive_failures = 1

        await batcher._reinject_failed_buffer(flush_buf)

        ht.record_event.assert_called_once_with("data_loss", table="hft.test_table", count=flush_buf.row_count)

    @pytest.mark.asyncio
    async def test_reinject_failure_calls_health_tracker(self, monkeypatch):
        """When reinject itself fails (exception), health_tracker.record_event is called."""
        ht = MagicMock()
        batcher = _make_batcher(monkeypatch, health_tracker=ht, reinject_max=5)
        flush_buf = _make_flush_buf()

        # Use a MagicMock as flush_buf since ColumnarBuffer has __slots__
        flush_buf = MagicMock()
        flush_buf.row_count = 2
        flush_buf.to_row_dicts = MagicMock(side_effect=RuntimeError("disk full"))

        await batcher._reinject_failed_buffer(flush_buf)

        ht.record_event.assert_called_once_with("data_loss", table="hft.test_table")

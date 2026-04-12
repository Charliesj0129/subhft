"""Tests for AuditWriter (WU-02)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hft_platform.recorder.audit import AuditWriter, get_audit_writer, reset_audit_writer


class TestAuditWriter:
    """Unit tests for AuditWriter."""

    def setup_method(self) -> None:
        reset_audit_writer()

    def teardown_method(self) -> None:
        reset_audit_writer()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def test_create_default_queues(self) -> None:
        writer = AuditWriter()
        assert len(writer._queues) == 3
        assert "audit.orders_log" in writer._queues
        assert "audit.risk_log" in writer._queues
        assert "audit.guardrail_log" in writer._queues

    def test_custom_queue_size(self) -> None:
        writer = AuditWriter(queue_size=42)
        for q in writer._queues.values():
            assert q.maxsize == 42

    # ------------------------------------------------------------------
    # Non-blocking put
    # ------------------------------------------------------------------

    def test_log_order_enqueues(self) -> None:
        writer = AuditWriter(queue_size=100)
        writer.log_order({"cmd_id": 1, "symbol": "2330"})
        assert writer._queues["audit.orders_log"].qsize() == 1

    def test_log_risk_decision_enqueues(self) -> None:
        writer = AuditWriter(queue_size=100)
        writer.log_risk_decision({"approved": True, "reason_code": "OK"})
        assert writer._queues["audit.risk_log"].qsize() == 1

    def test_log_guardrail_transition_enqueues(self) -> None:
        writer = AuditWriter(queue_size=100)
        writer.log_guardrail_transition({"old_state": "NORMAL", "new_state": "HALT"})
        assert writer._queues["audit.guardrail_log"].qsize() == 1

    def test_log_order_adds_ts_ns(self) -> None:
        writer = AuditWriter(queue_size=100)
        writer.log_order({"cmd_id": 1})
        item = writer._queues["audit.orders_log"].get_nowait()
        assert "ts_ns" in item
        assert isinstance(item["ts_ns"], int)

    def test_overflow_on_queue_full(self) -> None:
        writer = AuditWriter(queue_size=2)
        writer.log_order({"a": 1})
        writer.log_order({"a": 2})
        writer.log_order({"a": 3})  # Goes to overflow buffer
        assert writer._queues["audit.orders_log"].qsize() == 2
        assert len(writer._overflow["audit.orders_log"]) == 1
        assert writer._dropped["audit.orders_log"] == 0

    def test_drop_after_overflow_exhausted(self) -> None:
        import os

        os.environ["HFT_AUDIT_OVERFLOW_SIZE"] = "2"
        try:
            writer = AuditWriter(queue_size=1)
            writer.log_order({"a": 1})  # queue
            writer.log_order({"a": 2})  # overflow[0]
            writer.log_order({"a": 3})  # overflow[1]
            writer.log_order({"a": 4})  # hard drop
            assert writer._queues["audit.orders_log"].qsize() == 1
            assert len(writer._overflow["audit.orders_log"]) == 2
            assert writer._dropped["audit.orders_log"] == 1
        finally:
            os.environ.pop("HFT_AUDIT_OVERFLOW_SIZE", None)

    # ------------------------------------------------------------------
    # Flush to writer
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_flush_writes_to_ch(self) -> None:
        mock_writer = AsyncMock()
        audit = AuditWriter(queue_size=100, flush_interval_ms=10, writer=mock_writer)
        audit.log_order({"cmd_id": 1})
        audit.log_order({"cmd_id": 2})

        await audit.start()
        # Give flush loop time to run (flush_interval=10ms, wait 5 cycles)
        await asyncio.sleep(0.05)
        await audit.stop()

        mock_writer.write.assert_called()
        call_args = mock_writer.write.call_args_list
        tables_written = [c.args[0] for c in call_args]
        assert "audit.orders_log" in tables_written

    @pytest.mark.asyncio
    async def test_flush_fallback_on_writer_error(self) -> None:
        """When ClickHouse writer fails, rows are logged via structlog (no data loss)."""
        mock_writer = AsyncMock()
        mock_writer.write.side_effect = ConnectionError("CH down")
        audit = AuditWriter(queue_size=100, flush_interval_ms=10, writer=mock_writer)
        audit.log_risk_decision({"approved": True})

        await audit.start()
        await asyncio.sleep(0.05)  # flush_interval=10ms, wait 5 cycles
        await audit.stop()

        # Writer was called and failed, but no exception propagated
        mock_writer.write.assert_called()

    @pytest.mark.asyncio
    async def test_flush_no_writer_uses_structlog(self) -> None:
        """With no writer configured, rows are flushed via structlog fallback."""
        audit = AuditWriter(queue_size=100, flush_interval_ms=10, writer=None)
        audit.log_guardrail_transition({"old_state": "NORMAL", "new_state": "HALT"})

        await audit.start()
        await asyncio.sleep(0.05)  # flush_interval=10ms, wait 5 cycles
        await audit.stop()
        # Structlog fallback drained all queues — all should be empty
        assert all(q.qsize() == 0 for q in audit._queues.values())

    @pytest.mark.asyncio
    async def test_stop_drains_remaining(self) -> None:
        mock_writer = AsyncMock()
        audit = AuditWriter(
            queue_size=100,
            flush_interval_ms=5000,  # Long interval so flush doesn't happen naturally
            writer=mock_writer,
        )
        audit.log_order({"cmd_id": 99})
        # Don't start, just drain
        await audit._drain("audit.orders_log")
        mock_writer.write.assert_called_once()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    def test_get_audit_writer_singleton(self) -> None:
        a = get_audit_writer()
        b = get_audit_writer()
        assert a is b

    def test_reset_audit_writer(self) -> None:
        a = get_audit_writer()
        reset_audit_writer()
        b = get_audit_writer()
        assert a is not b

    # ------------------------------------------------------------------
    # Dropped counts observability
    # ------------------------------------------------------------------

    def test_dropped_counts_property(self) -> None:
        import os

        os.environ["HFT_AUDIT_OVERFLOW_SIZE"] = "1"
        try:
            writer = AuditWriter(queue_size=1)
            writer.log_order({"a": 1})  # queue
            writer.log_order({"a": 2})  # overflow
            writer.log_order({"a": 3})  # hard drop
            counts = writer.dropped_counts
            assert counts["audit.orders_log"] == 1
            assert counts["audit.risk_log"] == 0
        finally:
            os.environ.pop("HFT_AUDIT_OVERFLOW_SIZE", None)

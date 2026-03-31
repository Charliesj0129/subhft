"""Tests for Batcher._wal_emergency_dump and the emergency WAL fallback
triggered in _write_flush_buffer exception handlers.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from hft_platform.recorder.batcher import Batcher, ColumnarBuffer, GlobalMemoryGuard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_batcher(writer, table_name: str = "hft.test_table") -> Batcher:
    GlobalMemoryGuard.reset()
    return Batcher(table_name=table_name, flush_limit=100, writer=writer)


def _fill_buffer(batcher: Batcher, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        batcher._active.append_row(row)


# ---------------------------------------------------------------------------
# _wal_emergency_dump: success path
# ---------------------------------------------------------------------------


class TestWalEmergencyDump:
    def test_creates_jsonl_file_in_wal_dir(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1000, "price": 200000}, {"ts": 2000, "price": 210000}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            batcher._wal_emergency_dump(batcher._active)

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        fname = files[0].name
        assert fname.startswith("emergency_")
        assert fname.endswith(".jsonl")
        # Table name must NOT appear in filename (batch format uses __wal_table__ header)
        assert "hft.test_table" not in fname

    def test_file_contains_correct_row_count(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"a": i, "b": i * 2} for i in range(5)]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            batcher._wal_emergency_dump(batcher._active)

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        lines = [ln for ln in files[0].read_text().splitlines() if ln.strip()]
        # 1 header line + 5 data rows
        assert len(lines) == 6
        header = json.loads(lines[0])
        assert header["__wal_table__"] == "hft.test_table"
        assert header["__row_count__"] == 5

    def test_file_is_valid_jsonl(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"x": 1, "y": "hello"}, {"x": 2, "y": "world"}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            batcher._wal_emergency_dump(batcher._active)

        files = list(tmp_path.iterdir())
        all_lines = [json.loads(ln) for ln in files[0].read_text().splitlines() if ln.strip()]
        # First line is the __wal_table__ header; remainder are the data rows
        assert "__wal_table__" in all_lines[0]
        assert all_lines[1:] == rows

    def test_empty_buffer_writes_no_file(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        # active buffer has 0 rows

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            batcher._wal_emergency_dump(batcher._active)

        assert list(tmp_path.iterdir()) == []

    def test_creates_wal_dir_if_missing(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"k": "v"}]
        _fill_buffer(batcher, rows)

        new_dir = tmp_path / "wal_subdir"
        assert not new_dir.exists()

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(new_dir)}):
            batcher._wal_emergency_dump(batcher._active)

        assert new_dir.exists()
        assert len(list(new_dir.iterdir())) == 1

    def test_does_not_raise_when_to_row_dicts_fails(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        buf = MagicMock(spec=ColumnarBuffer)
        buf.to_row_dicts.side_effect = RuntimeError("boom")

        # Must not raise
        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            batcher._wal_emergency_dump(buf)

        assert list(tmp_path.iterdir()) == []

    def test_does_not_raise_when_file_write_fails(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"k": "v"}]
        _fill_buffer(batcher, rows)

        # Make makedirs succeed but open() fail
        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("builtins.open", side_effect=OSError("disk full")):
                batcher._wal_emergency_dump(batcher._active)  # must not raise

    def test_returns_true_on_success(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            result = batcher._wal_emergency_dump(batcher._active)

        assert result is True

    def test_returns_true_on_empty_buffer(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            result = batcher._wal_emergency_dump(batcher._active)

        assert result is True

    def test_returns_false_on_file_write_failure(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"k": "v"}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("builtins.open", side_effect=OSError("disk full")):
                result = batcher._wal_emergency_dump(batcher._active)

        assert result is False

    def test_returns_false_on_row_extract_failure(self, tmp_path) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        buf = MagicMock(spec=ColumnarBuffer)
        buf.to_row_dicts.side_effect = RuntimeError("boom")

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            result = batcher._wal_emergency_dump(buf)

        assert result is False


# ---------------------------------------------------------------------------
# _write_flush_buffer: exception handlers call emergency dump
# ---------------------------------------------------------------------------


class TestWriteFlushBufferEmergencyFallback:
    """Verify emergency dump is triggered on write failures."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_timeout_error_triggers_emergency_dump(self, tmp_path) -> None:
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=asyncio.TimeoutError())
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "price": 100}, {"ts": 2, "price": 200}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name.startswith("emergency_")

    def test_connection_error_triggers_emergency_dump(self, tmp_path) -> None:
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=ConnectionError("refused"))
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "val": 42}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        files = list(tmp_path.iterdir())
        assert len(files) == 1

    def test_generic_exception_triggers_emergency_dump(self, tmp_path) -> None:
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=RuntimeError("unexpected"))
        batcher = _make_batcher(writer)
        rows = [{"a": 1}, {"a": 2}, {"a": 3}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        lines = [ln for ln in files[0].read_text().splitlines() if ln.strip()]
        # 1 header line + 3 data rows
        assert len(lines) == 4

    def test_emergency_dump_file_contains_correct_data(self, tmp_path) -> None:
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=RuntimeError("write failure"))
        batcher = _make_batcher(writer)
        rows = [{"ts": 111, "price": 500000}, {"ts": 222, "price": 510000}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        files = list(tmp_path.iterdir())
        all_lines = [json.loads(ln) for ln in files[0].read_text().splitlines() if ln.strip()]
        # First line is the __wal_table__ header; remainder are the data rows
        assert "__wal_table__" in all_lines[0]
        assert all_lines[0]["__wal_table__"] == "hft.test_table"
        assert all_lines[1:] == rows

    def test_buffer_cleared_after_successful_emergency_dump(self, tmp_path) -> None:
        """flush_buf.clear() runs when emergency WAL dump succeeds."""
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=ConnectionError("down"))
        batcher = _make_batcher(writer)
        rows = [{"k": "v"}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        assert flush_buf.row_count == 0

    def test_no_emergency_dump_on_success(self, tmp_path) -> None:
        """No emergency file created when write succeeds."""
        writer = MagicMock()
        writer.write_columnar = AsyncMock(return_value=None)
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "price": 100}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            self._run(batcher._write_flush_buffer(flush_buf))

        assert list(tmp_path.iterdir()) == []

    def test_double_fault_reinjects_rows_into_active_buffer(self, tmp_path) -> None:
        """When both CH write and WAL dump fail, rows are re-injected for retry."""
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=RuntimeError("primary fail"))
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "price": 100}, {"ts": 2, "price": 200}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        # Ensure active buffer is empty before re-injection
        batcher._active = batcher._new_buffer()

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("builtins.open", side_effect=OSError("disk full")):
                # Must not raise despite double fault
                self._run(batcher._write_flush_buffer(flush_buf))

        # Rows re-injected into active buffer, NOT cleared from flush_buf
        assert batcher._active.row_count == 2
        reinjected = batcher._active.to_row_dicts()
        assert reinjected == rows

    def test_double_fault_does_not_crash_recorder(self, tmp_path) -> None:
        """Even if emergency dump and reinject both fail, recorder keeps running."""
        writer = MagicMock()
        writer.write_columnar = AsyncMock(side_effect=RuntimeError("primary fail"))
        batcher = _make_batcher(writer)
        rows = [{"ts": 1}]
        _fill_buffer(batcher, rows)

        # Use a MagicMock buffer that fails on second to_row_dicts call (reinject)
        flush_buf = MagicMock(spec=ColumnarBuffer)
        flush_buf.row_count = 1
        flush_buf.to_row_dicts = MagicMock(side_effect=[rows, RuntimeError("extract fail")])

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("builtins.open", side_effect=OSError("disk full")):
                # Must not raise despite triple fault
                self._run(batcher._write_flush_buffer(flush_buf))


# ---------------------------------------------------------------------------
# _reinject_failed_buffer
# ---------------------------------------------------------------------------


class TestReinjectFailedBuffer:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_reinjects_rows_into_active_buffer(self) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "price": 100}, {"ts": 2, "price": 200}]
        _fill_buffer(batcher, rows)
        flush_buf = batcher._active

        # Reset active to empty
        batcher._active = batcher._new_buffer()
        assert batcher._active.row_count == 0

        self._run(batcher._reinject_failed_buffer(flush_buf))

        assert batcher._active.row_count == 2
        assert batcher._active.to_row_dicts() == rows

    def test_reinject_updates_memory_guard(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100000)
        writer = MagicMock()
        batcher = Batcher(
            table_name="hft.test",
            flush_limit=100,
            writer=writer,
            memory_guard=guard,
        )
        rows = [{"a": 1}, {"a": 2}, {"a": 3}]
        flush_buf = batcher._new_buffer()
        for r in rows:
            flush_buf.append_row(r)

        before = guard.total_rows
        self._run(batcher._reinject_failed_buffer(flush_buf))

        assert guard.total_rows == before + 3

    def test_reinject_empty_buffer_is_noop(self) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        empty_buf = batcher._new_buffer()

        self._run(batcher._reinject_failed_buffer(empty_buf))
        assert batcher._active.row_count == 0

    def test_reinject_does_not_raise_on_failure(self) -> None:
        writer = MagicMock()
        batcher = _make_batcher(writer)
        buf = MagicMock(spec=ColumnarBuffer)
        buf.to_row_dicts.side_effect = RuntimeError("extract fail")

        # Must not raise
        self._run(batcher._reinject_failed_buffer(buf))

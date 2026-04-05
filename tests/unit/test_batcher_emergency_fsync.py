"""Tests that _wal_emergency_dump calls f.flush() and os.fsync() for crash durability."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

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
# fsync durability tests
# ---------------------------------------------------------------------------


class TestEmergencyWalFsync:
    """Verify that os.fsync() is called after f.flush() in _wal_emergency_dump."""

    def test_fsync_called_on_successful_dump(self, tmp_path) -> None:
        """os.fsync must be called exactly once per successful emergency dump."""
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1000, "price": 200000}, {"ts": 2000, "price": 210000}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("os.fsync") as mock_fsync:
                result = batcher._wal_emergency_dump(batcher._active)

        assert result is True
        mock_fsync.assert_called_once()

    def test_flush_called_before_fsync(self, tmp_path) -> None:
        """f.flush() must be called before os.fsync() — order matters for durability."""
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1}, {"ts": 2}]
        _fill_buffer(batcher, rows)

        call_order: list[str] = []

        real_open = open

        def tracking_open(path, mode="r", **kwargs):
            fh = real_open(path, mode, **kwargs)
            original_flush = fh.flush
            original_fileno = fh.fileno

            def tracked_flush():
                call_order.append("flush")
                return original_flush()

            def tracked_fileno():
                return original_fileno()

            fh.flush = tracked_flush  # type: ignore[method-assign]
            fh.fileno = tracked_fileno  # type: ignore[method-assign]
            return fh

        def tracked_fsync(fd: int) -> None:
            call_order.append("fsync")

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("builtins.open", side_effect=tracking_open):
                with patch("os.fsync", side_effect=tracked_fsync):
                    batcher._wal_emergency_dump(batcher._active)

        assert "flush" in call_order, "f.flush() was not called"
        assert "fsync" in call_order, "os.fsync() was not called"
        assert call_order.index("flush") < call_order.index("fsync"), (
            f"flush must precede fsync, got order: {call_order}"
        )

    def test_fsync_not_called_on_empty_buffer(self, tmp_path) -> None:
        """No file is written for an empty buffer — fsync must not be called."""
        writer = MagicMock()
        batcher = _make_batcher(writer)
        # active buffer has 0 rows

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("os.fsync") as mock_fsync:
                result = batcher._wal_emergency_dump(batcher._active)

        assert result is True
        mock_fsync.assert_not_called()

    def test_fsync_failure_does_not_raise(self, tmp_path) -> None:
        """If os.fsync() raises OSError, _wal_emergency_dump must not propagate it."""
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 1, "price": 100000}]
        _fill_buffer(batcher, rows)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("os.fsync", side_effect=OSError("fsync failed")):
                # Must not raise — the method MUST NOT raise per its contract
                result = batcher._wal_emergency_dump(batcher._active)

        assert result is False

    def test_fsync_receives_valid_file_descriptor(self, tmp_path) -> None:
        """os.fsync() must be called with the file's actual fd (an integer)."""
        writer = MagicMock()
        batcher = _make_batcher(writer)
        rows = [{"ts": 99, "val": 42}]
        _fill_buffer(batcher, rows)

        captured_fds: list[int] = []

        def capture_fsync(fd: int) -> None:
            captured_fds.append(fd)

        with patch.dict(os.environ, {"HFT_WAL_DIR": str(tmp_path)}):
            with patch("os.fsync", side_effect=capture_fsync):
                batcher._wal_emergency_dump(batcher._active)

        assert len(captured_fds) == 1
        assert isinstance(captured_fds[0], int), (
            f"Expected int fd, got {type(captured_fds[0])}"
        )
        assert captured_fds[0] > 0, "File descriptor must be a positive integer"

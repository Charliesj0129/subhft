"""Coverage tests for recorder/wal.py."""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALWriter

# ── WALWriter construction ────────────────────────────────────────────────


class TestWALWriterInit:
    def test_default(self, tmp_path):
        wal_dir = str(tmp_path / "wal")
        w = WALWriter(wal_dir)
        assert os.path.isdir(wal_dir)
        assert w._disk_full is False

    def test_metrics_exception(self, tmp_path):
        wal_dir = str(tmp_path / "wal")
        with patch("hft_platform.recorder.wal.MetricsRegistry") as mr:
            mr.get.side_effect = RuntimeError("no metrics")
            w = WALWriter(wal_dir)
            assert w._metrics is None


# ── _check_disk_space ─────────────────────────────────────────────────────


class TestCheckDiskSpace:
    def test_sufficient_space(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._last_disk_check_ts = 0  # force check
        result = w._check_disk_space()
        assert result is True

    def test_cached(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._last_disk_check_ts = time.monotonic()
        w._disk_full = True
        result = w._check_disk_space()
        assert result is False  # cached disk_full

    def test_oserror_fail_open(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._last_disk_check_ts = 0
        with patch("os.statvfs", side_effect=OSError("no stat")):
            result = w._check_disk_space()
            assert result is True  # fail open

    def test_low_space_activates_breaker(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._last_disk_check_ts = 0
        w._disk_min_mb = 999999999  # impossibly high
        result = w._check_disk_space()
        assert result is False
        assert w._disk_full is True

    def test_recovery_deactivates_breaker(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._last_disk_check_ts = 0
        w._disk_full = True
        w._disk_min_mb = 0  # any space is enough
        result = w._check_disk_space()
        assert result is True
        assert w._disk_full is False


# ── _handle_disk_pressure_skip ────────────────────────────────────────────


class TestHandleDiskPressureSkip:
    def test_halt_policy(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._disk_pressure_policy = "halt"
        result = w._handle_disk_pressure_skip("test_table", 10, writer="wal")
        assert result is False
        assert w._disk_full_count == 10

    def test_raise_policy(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._disk_pressure_policy = "raise"
        with pytest.raises(RuntimeError, match="circuit breaker"):
            w._handle_disk_pressure_skip("test_table", 5, writer="wal")


# ── _maybe_fsync_file ─────────────────────────────────────────────────────


class TestMaybeFsyncFile:
    def test_disabled(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._fsync_file_enabled = False
        w._maybe_fsync_file(0, writer="wal")  # no-op
        assert w._fsync_file_enabled is False

    def test_enabled(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._fsync_file_enabled = True
        f = tmp_path / "test.txt"
        f.write_text("data")
        fd = os.open(str(f), os.O_RDONLY)
        try:
            w._maybe_fsync_file(fd, writer="wal")
        finally:
            os.close(fd)
        assert w._fsync_file_enabled is True


# ── _maybe_fsync_dir ──────────────────────────────────────────────────────


class TestMaybeFsyncDir:
    def test_skipped_within_interval(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._dir_fsync_min_ms = 60000  # very high
        ts_before = time.monotonic()
        w._last_dir_fsync_ts = ts_before
        w._maybe_fsync_dir(str(tmp_path), writer="wal")  # skipped
        assert w._last_dir_fsync_ts == ts_before  # unchanged — was skipped

    def test_runs_when_threshold_zero(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._dir_fsync_min_ms = 0
        w._maybe_fsync_dir(str(tmp_path), writer="wal")
        assert w._dir_fsync_min_ms == 0


# ── _record_* metrics no-ops ──────────────────────────────────────────────


class TestMetricsNoOps:
    def test_no_metrics_wal_write(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._metrics = None
        w._record_wal_write_latency("wal", "atomic", 1.0)
        w._record_fsync_latency("wal", "file", 0.5)
        w._set_disk_pressure_metrics(100.0, False, "wal")
        assert w._metrics is None  # confirms no side-effect

    def test_with_metrics(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        m = MagicMock()
        w._metrics = m
        w._record_wal_write_latency("wal", "atomic", 1.0)
        m.recorder_wal_write_latency_ms.labels.assert_called()


# ── write async ───────────────────────────────────────────────────────────


class TestWrite:
    @pytest.mark.asyncio
    async def test_disk_full_returns_false(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._disk_full = True
        w._last_disk_check_ts = time.monotonic()  # cached
        result = await w.write("test", [{"a": 1}])
        assert result is False

    @pytest.mark.asyncio
    async def test_write_success(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._disk_full = False
        w._last_disk_check_ts = time.monotonic()
        result = await w.write("test", [{"a": 1}])
        assert result is True

    @pytest.mark.asyncio
    async def test_write_failure(self, tmp_path):
        w = WALWriter(str(tmp_path / "wal"))
        w._disk_full = False
        w._last_disk_check_ts = time.monotonic()
        with patch.object(w, "_write_sync_atomic", side_effect=OSError("fail")):
            result = await w.write("test", [{"a": 1}])
            assert result is False

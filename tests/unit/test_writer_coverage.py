"""Coverage gap tests for recorder/writer.py.

Targets uncovered branches: connect retry/backoff, schema init failure,
native-to-HTTP fallback, columnar chunking, timestamp sanitization,
write fallback to WAL, shutdown drain, heartbeat thread, reconnect
scheduling, and status reporting.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.writer import DataWriter, WriterDoubleFaultError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def writer(tmp_path):
    """DataWriter with ClickHouse disabled and WAL dir in tmp."""
    with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False):
        w = DataWriter(ch_host="localhost", ch_port=8123, wal_dir=str(tmp_path / "wal"))
    return w


# ---------------------------------------------------------------------------
# __init__ and env var parsing
# ---------------------------------------------------------------------------


def test_deprecated_disable_clickhouse_env(tmp_path):
    """HFT_DISABLE_CLICKHOUSE triggers deprecation warning."""
    with patch.dict(os.environ, {
        "HFT_DISABLE_CLICKHOUSE": "1",
        "HFT_CLICKHOUSE_ENABLED": "",
    }, clear=False):
        with pytest.warns(DeprecationWarning, match="deprecated"):
            w = DataWriter(ch_host="localhost", ch_port=8123, wal_dir=str(tmp_path / "wal"))
        assert w.ch_enabled is False


def test_env_port_override_native(tmp_path):
    """HFT_CLICKHOUSE_PORT=9000 sets native interface."""
    with patch.dict(os.environ, {
        "HFT_CLICKHOUSE_ENABLED": "0",
        "HFT_CLICKHOUSE_PORT": "9000",
    }, clear=False):
        w = DataWriter(ch_host="localhost", ch_port=8123, wal_dir=str(tmp_path / "wal"))
    assert w.ch_params.get("interface") == "native"


def test_env_port_override_http(tmp_path):
    """HFT_CLICKHOUSE_PORT=8123 removes native interface."""
    with patch.dict(os.environ, {
        "HFT_CLICKHOUSE_ENABLED": "0",
        "HFT_CLICKHOUSE_PORT": "8123",
    }, clear=False):
        w = DataWriter(ch_host="localhost", ch_port=9000, wal_dir=str(tmp_path / "wal"))
    assert "interface" not in w.ch_params


# ---------------------------------------------------------------------------
# _is_native_interface_unsupported_error
# ---------------------------------------------------------------------------


def test_native_interface_error_detection():
    assert DataWriter._is_native_interface_unsupported_error(
        RuntimeError("unrecognized client type native something")
    )
    assert not DataWriter._is_native_interface_unsupported_error(
        RuntimeError("connection refused")
    )


# ---------------------------------------------------------------------------
# _maybe_fallback_clickhouse_interface
# ---------------------------------------------------------------------------


def test_fallback_interface_on_native_error(writer):
    """Fallback from native to HTTP on supported error."""
    writer.ch_params["interface"] = "native"
    writer.ch_params["port"] = 9000
    exc = RuntimeError("unrecognized client type native")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is True
    assert "interface" not in writer.ch_params
    assert writer._native_interface_fallback_used is True


def test_no_fallback_when_not_native(writer):
    """No fallback when already using HTTP."""
    writer.ch_params.pop("interface", None)
    exc = RuntimeError("unrecognized client type native")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is False


def test_no_fallback_on_unrelated_error(writer):
    """No fallback on unrelated errors."""
    writer.ch_params["interface"] = "native"
    exc = RuntimeError("connection timeout")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is False


# ---------------------------------------------------------------------------
# _compute_backoff_delay
# ---------------------------------------------------------------------------


def test_compute_backoff_delay(writer):
    d0 = writer._compute_backoff_delay(0)
    assert d0 >= 0.1  # minimum floor
    d5 = writer._compute_backoff_delay(5)
    assert d5 <= writer._max_backoff_s * 2  # bounded


# ---------------------------------------------------------------------------
# connect: ClickHouse disabled
# ---------------------------------------------------------------------------


def test_connect_disabled(writer):
    """connect() is a no-op when CH is disabled."""
    writer.ch_enabled = False
    writer.connect()
    assert not writer.connected


# ---------------------------------------------------------------------------
# _should_log_insert_success
# ---------------------------------------------------------------------------


def test_should_log_insert_success_flag(writer):
    writer._ch_insert_log_success = True
    assert writer._should_log_insert_success(1) is True


def test_should_log_insert_success_every_n(writer):
    writer._ch_insert_log_success = False
    writer._ch_insert_log_every = 5
    assert writer._should_log_insert_success(5) is True
    assert writer._should_log_insert_success(3) is False


def test_should_log_insert_success_disabled(writer):
    writer._ch_insert_log_success = False
    writer._ch_insert_log_every = 0
    assert writer._should_log_insert_success(1) is False


# ---------------------------------------------------------------------------
# _iter_row_chunks / _iter_columnar_chunks
# ---------------------------------------------------------------------------


def test_iter_row_chunks_no_chunking(writer):
    writer._ch_insert_chunk_rows = 0
    data = [{"a": 1}, {"a": 2}]
    chunks = writer._iter_row_chunks(data)
    assert len(chunks) == 1
    assert chunks[0] is data


def test_iter_row_chunks_with_chunking(writer):
    writer._ch_insert_chunk_rows = 2
    data = [{"a": i} for i in range(5)]
    chunks = writer._iter_row_chunks(data)
    assert len(chunks) == 3  # 2+2+1


def test_iter_columnar_chunks_no_chunking(writer):
    writer._ch_insert_chunk_rows = 0
    cols = [[1, 2, 3], [4, 5, 6]]
    chunks = writer._iter_columnar_chunks(cols, 3)
    assert len(chunks) == 1


def test_iter_columnar_chunks_with_chunking(writer):
    writer._ch_insert_chunk_rows = 2
    cols = [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
    chunks = writer._iter_columnar_chunks(cols, 5)
    assert len(chunks) == 3  # 2+2+1


# ---------------------------------------------------------------------------
# _transpose_columnar_rows / _columnar_to_row_dicts
# ---------------------------------------------------------------------------


def test_transpose_columnar_rows():
    cols = [[1, 2], [3, 4]]
    result = DataWriter._transpose_columnar_rows(cols, 2)
    assert result == [[1, 3], [2, 4]]


def test_transpose_columnar_rows_empty():
    assert DataWriter._transpose_columnar_rows([], 0) == []


def test_columnar_to_row_dicts():
    cols = [[1, 2], [3, 4]]
    result = DataWriter._columnar_to_row_dicts(["a", "b"], cols, 2)
    assert result == [{"a": 1, "b": 3}, {"a": 2, "b": 4}]


def test_columnar_to_row_dicts_empty():
    assert DataWriter._columnar_to_row_dicts([], [], 0) == []


# ---------------------------------------------------------------------------
# _sanitize_timestamps
# ---------------------------------------------------------------------------


def test_sanitize_timestamps_no_filter(writer):
    """When _ts_max_future_ns is 0, no rows are dropped."""
    writer._ts_max_future_ns = 0
    data = [{"exch_ts": 100, "ingest_ts": 200}]
    result = writer._sanitize_timestamps("test", data)
    assert len(result) == 1


def test_sanitize_timestamps_ingest_before_exch(writer):
    """ingest_ts < exch_ts gets corrected."""
    writer._ts_max_future_ns = 0
    data = [{"exch_ts": 200, "ingest_ts": 100}]
    result = writer._sanitize_timestamps("test", data)
    assert result[0]["ingest_ts"] == 200


def test_sanitize_timestamps_future_dropped(writer):
    """Far-future timestamps are dropped."""
    writer._ts_max_future_ns = 1_000_000_000  # 1s
    far_future = time.time_ns() + 999_000_000_000_000
    data = [{"exch_ts": far_future, "ingest_ts": far_future}]
    result = writer._sanitize_timestamps("test", data)
    assert len(result) == 0


def test_sanitize_timestamps_empty(writer):
    result = writer._sanitize_timestamps("test", [])
    assert result == []


# ---------------------------------------------------------------------------
# _sanitize_columnar
# ---------------------------------------------------------------------------


def test_sanitize_columnar_no_ts_columns(writer):
    cols = [["a", "b"]]
    result_cols, count = writer._sanitize_columnar("test", ["name"], cols, 2)
    assert count == 2


def test_sanitize_columnar_corrects_ingest(writer):
    writer._ts_max_future_ns = 0
    cols = [[200, 300], [100, 300]]  # exch_ts, ingest_ts; first row ingest < exch
    result_cols, count = writer._sanitize_columnar("test", ["exch_ts", "ingest_ts"], cols, 2)
    assert count == 2
    assert result_cols[1][0] == 200  # Corrected


def test_sanitize_columnar_drops_future(writer):
    writer._ts_max_future_ns = 1_000_000_000
    far_future = time.time_ns() + 999_000_000_000_000
    cols = [[far_future], [far_future]]
    result_cols, count = writer._sanitize_columnar("test", ["exch_ts", "ingest_ts"], cols, 1)
    assert count == 0


def test_sanitize_columnar_empty(writer):
    cols, count = writer._sanitize_columnar("test", ["exch_ts"], [[]], 0)
    assert count == 0


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status(writer):
    status = writer.get_status()
    assert "ch_enabled" in status
    assert "connected" in status
    assert "wal_only_mode" in status
    assert status["wal_only_mode"] is True  # Not connected


# ---------------------------------------------------------------------------
# write: row-dict interface with WAL fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_empty_data(writer):
    """write() is a no-op for empty data."""
    await writer.write("test", [])
    # Should not raise


@pytest.mark.asyncio
async def test_write_wal_fallback_success(writer):
    """When CH is disconnected, write falls back to WAL."""
    writer.connected = False
    writer.ch_enabled = False
    data = [{"exch_ts": 100, "val": 1}]
    await writer.write("test", data)
    # Should not raise; WAL write succeeds


@pytest.mark.asyncio
async def test_write_columnar_empty(writer):
    """write_columnar with empty data is a no-op."""
    await writer.write_columnar("test", [], [], 0)


@pytest.mark.asyncio
async def test_write_columnar_wal_fallback(writer):
    """When CH disconnected, write_columnar falls back to WAL."""
    writer.connected = False
    writer.ch_enabled = False
    cols = [[100], [200]]
    await writer.write_columnar("test", ["a", "b"], cols, 1)


# ---------------------------------------------------------------------------
# _do_heartbeat_check
# ---------------------------------------------------------------------------


def test_heartbeat_check_no_client(writer):
    writer.ch_client = None
    assert writer._do_heartbeat_check() is False


def test_heartbeat_check_exception(writer):
    writer.ch_client = MagicMock()
    writer.ch_client.command.side_effect = RuntimeError("connection lost")
    assert writer._do_heartbeat_check() is False


# ---------------------------------------------------------------------------
# _schedule_reconnect
# ---------------------------------------------------------------------------


def test_schedule_reconnect_disabled(writer):
    writer.ch_enabled = False
    writer._schedule_reconnect("test")
    # Should be a no-op


def test_schedule_reconnect_rate_limited(writer):
    writer.ch_enabled = True
    writer._last_reconnect_ts = time.time() + 9999  # Far future
    writer._schedule_reconnect("test")
    # Should be rate-limited


# ---------------------------------------------------------------------------
# _get_table_lock
# ---------------------------------------------------------------------------


def test_get_table_lock_caching(writer):
    lock1 = writer._get_table_lock("tbl1")
    lock2 = writer._get_table_lock("tbl1")
    assert lock1 is lock2


def test_get_table_lock_different_tables(writer):
    lock1 = writer._get_table_lock("tbl1")
    lock2 = writer._get_table_lock("tbl2")
    assert lock1 is not lock2


# ---------------------------------------------------------------------------
# set_health_tracker
# ---------------------------------------------------------------------------


def test_set_health_tracker(writer):
    tracker = MagicMock()
    writer.set_health_tracker(tracker)
    assert writer._health_tracker is tracker


# ---------------------------------------------------------------------------
# _init_schema
# ---------------------------------------------------------------------------


def test_init_schema_failure(writer):
    """Schema init failure sets connected=False."""
    writer.ch_client = MagicMock()
    with patch("hft_platform.recorder.writer.apply_schema", side_effect=RuntimeError("schema fail")):
        writer._init_schema()
    assert writer._schema_initialized is False
    assert writer.connected is False


def test_init_schema_view_repair_failure(writer):
    """View repair failure doesn't fully fail."""
    writer.ch_client = MagicMock()
    with patch("hft_platform.recorder.writer.apply_schema"):
        with patch("hft_platform.recorder.writer.ensure_price_scaled_views", side_effect=RuntimeError("view fail")):
            writer._init_schema()
    assert writer._schema_initialized is True  # Schema still OK


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_no_inflight(writer):
    """shutdown completes quickly when no in-flight inserts."""
    await writer.shutdown()
    # Should not raise


@pytest.mark.asyncio
async def test_shutdown_with_wal_batch_writer(writer):
    """shutdown flushes and stops WAL batch writer."""
    mock_bw = MagicMock()

    async def _flush():
        pass

    mock_bw.flush = _flush
    mock_bw.stop = MagicMock()
    writer._wal_batch_writer = mock_bw
    await writer.shutdown()
    mock_bw.stop.assert_called_once()


# ---------------------------------------------------------------------------
# WriterDoubleFaultError
# ---------------------------------------------------------------------------


def test_writer_double_fault_error():
    err = WriterDoubleFaultError("test")
    assert str(err) == "test"
    assert isinstance(err, Exception)

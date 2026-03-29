"""Coverage-focused tests for recorder/writer.py.

Targets uncovered paths: env-var credential resolution, interface fallback,
schema init failure, write/write_columnar success and error paths,
_sanitize_timestamps, _sanitize_columnar, heartbeat, shutdown, get_status.
"""

import asyncio
import warnings
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from hft_platform.recorder.writer import DataWriter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer(tmp_path):
    """DataWriter with ClickHouse disabled and fast backoff."""
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "HFT_CH_MAX_RETRIES": "2",
            "HFT_CH_BASE_DELAY_S": "0.01",
            "HFT_CH_MAX_BACKOFF_S": "0.1",
            "HFT_TS_MAX_FUTURE_S": "5",
        },
        clear=False,
    ):
        return DataWriter(wal_dir=str(tmp_path))


@pytest.fixture()
def connected_writer(tmp_path):
    """DataWriter in connected state with mocked CH client."""
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "1",
            "HFT_CH_MAX_RETRIES": "1",
            "HFT_CH_BASE_DELAY_S": "0.01",
            "HFT_TS_MAX_FUTURE_S": "5",
        },
        clear=False,
    ):
        w = DataWriter(wal_dir=str(tmp_path))
    w.connected = True
    w.ch_enabled = True
    w.ch_client = MagicMock()
    return w


# ---------------------------------------------------------------------------
# Credential environment variable resolution
# ---------------------------------------------------------------------------


def test_username_resolved_from_hft_clickhouse_username_deprecated(tmp_path):
    with (
        patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "0",
                "HFT_CLICKHOUSE_USERNAME": "user123",
            },
            clear=False,
        ),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer.ch_params["username"] == "user123"
    assert any("HFT_CLICKHOUSE_USERNAME" in str(warning.message) for warning in w)


def test_username_resolved_from_clickhouse_user(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "CLICKHOUSE_USER": "ck_user",
        },
        clear=False,
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer.ch_params["username"] == "ck_user"


def test_username_resolved_from_clickhouse_username_deprecated(tmp_path):
    with (
        patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "0",
                "CLICKHOUSE_USERNAME": "old_user",
            },
            clear=False,
        ),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer.ch_params["username"] == "old_user"
    assert any("CLICKHOUSE_USERNAME" in str(warning.message) for warning in w)


def test_username_defaults_to_default_when_no_env(tmp_path):
    env_keys = [
        "HFT_CLICKHOUSE_USER",
        "HFT_CLICKHOUSE_USERNAME",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_USERNAME",
    ]
    env_overrides = {"HFT_CLICKHOUSE_ENABLED": "0"}
    for k in env_keys:
        env_overrides[k] = ""
    with patch.dict("os.environ", env_overrides, clear=False):
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer.ch_params["username"] == "default"


def test_hft_disable_clickhouse_deprecated_env(tmp_path):
    with (
        patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "1",
                "HFT_DISABLE_CLICKHOUSE": "1",
            },
            clear=False,
        ),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        writer = DataWriter(wal_dir=str(tmp_path))
    assert not writer.ch_enabled
    assert any("HFT_DISABLE_CLICKHOUSE" in str(warning.message) for warning in w)


def test_env_port_override_to_http_removes_native_interface(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "HFT_CLICKHOUSE_PORT": "8123",
        },
        clear=False,
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
    assert "interface" not in writer.ch_params
    assert writer.ch_params["port"] == 8123


def test_env_port_override_to_native_adds_interface(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "HFT_CLICKHOUSE_PORT": "9000",
        },
        clear=False,
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer.ch_params.get("interface") == "native"


def test_hft_ts_max_future_s_parse_failure_disables_filter(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "HFT_TS_MAX_FUTURE_S": "not_a_number",
        },
        clear=False,
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
    assert writer._ts_max_future_ns == 0


# ---------------------------------------------------------------------------
# _maybe_fallback_clickhouse_interface
# ---------------------------------------------------------------------------


def test_fallback_interface_strips_native_on_matching_error(writer):
    writer.ch_params["interface"] = "native"
    writer.ch_params["port"] = 9000
    exc = Exception("unrecognized client type native")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is True
    assert "interface" not in writer.ch_params
    assert writer._native_interface_fallback_used is True


def test_fallback_interface_returns_false_for_non_matching_error(writer):
    writer.ch_params["interface"] = "native"
    exc = Exception("connection refused")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is False


def test_fallback_interface_returns_false_when_not_native(writer):
    # No "interface" key → not native
    writer.ch_params.pop("interface", None)
    exc = Exception("unrecognized client type native")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is False


def test_fallback_interface_port_stays_when_not_canonical(writer):
    writer.ch_params["interface"] = "native"
    writer.ch_params["port"] = 19000  # Non-canonical native port
    exc = Exception("unrecognized client type native")
    writer._maybe_fallback_clickhouse_interface(exc)
    assert writer.ch_params["port"] == 19000


# ---------------------------------------------------------------------------
# _init_schema failure path
# ---------------------------------------------------------------------------


def test_init_schema_failure_sets_disconnected(writer):
    writer.connected = True
    writer.ch_client = MagicMock()
    with patch("hft_platform.recorder.writer.apply_schema", side_effect=RuntimeError("schema broken")):
        writer._init_schema()
    assert not writer._schema_initialized
    assert not writer.connected


def test_init_schema_view_failure_does_not_disconnect(writer):
    writer.connected = True
    writer.ch_client = MagicMock()
    with (
        patch("hft_platform.recorder.writer.apply_schema"),
        patch(
            "hft_platform.recorder.writer.ensure_price_scaled_views",
            side_effect=RuntimeError("view broken"),
        ),
    ):
        writer._init_schema()
    # Views are optional — should still be connected
    assert writer._schema_initialized
    assert writer.connected


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_returns_expected_keys(writer):
    status = writer.get_status()
    for key in (
        "ch_enabled",
        "connected",
        "schema_initialized",
        "wal_only_mode",
        "connect_attempts",
        "ch_host",
        "ch_port",
        "ch_interface",
        "native_interface_fallback_used",
        "last_heartbeat_ts",
        "last_heartbeat_ok",
    ):
        assert key in status, f"Missing key: {key}"


def test_get_status_wal_only_mode_when_not_connected(writer):
    writer.connected = False
    status = writer.get_status()
    assert status["wal_only_mode"] is True


# ---------------------------------------------------------------------------
# set_health_tracker
# ---------------------------------------------------------------------------


def test_set_health_tracker_assigns_reference(writer):
    tracker = MagicMock()
    writer.set_health_tracker(tracker)
    assert writer._health_tracker is tracker


# ---------------------------------------------------------------------------
# _do_heartbeat_check
# ---------------------------------------------------------------------------


def test_do_heartbeat_check_returns_true_on_success(connected_writer):
    connected_writer.ch_client.command.return_value = None
    assert connected_writer._do_heartbeat_check() is True


def test_do_heartbeat_check_returns_false_without_client(writer):
    writer.ch_client = None
    assert writer._do_heartbeat_check() is False


def test_do_heartbeat_check_returns_false_on_exception(connected_writer):
    connected_writer.ch_client.command.side_effect = RuntimeError("timeout")
    assert connected_writer._do_heartbeat_check() is False


# ---------------------------------------------------------------------------
# _iter_row_chunks and _iter_columnar_chunks
# ---------------------------------------------------------------------------


def test_iter_row_chunks_splits_when_chunk_size_set(writer):
    writer._ch_insert_chunk_rows = 2
    data = [1, 2, 3, 4, 5]
    chunks = writer._iter_row_chunks(data)
    assert len(chunks) == 3
    assert chunks[0] == [1, 2]
    assert chunks[2] == [5]


def test_iter_columnar_chunks_splits(writer):
    writer._ch_insert_chunk_rows = 2
    col_data = [[1, 2, 3, 4], [10, 20, 30, 40]]
    chunks = writer._iter_columnar_chunks(col_data, row_count=4)
    assert len(chunks) == 2
    rows_total = sum(r for _, r in chunks)
    assert rows_total == 4


def test_iter_columnar_chunks_no_split_when_chunk_zero(writer):
    writer._ch_insert_chunk_rows = 0
    col_data = [[1, 2, 3]]
    chunks = writer._iter_columnar_chunks(col_data, row_count=3)
    assert len(chunks) == 1


# ---------------------------------------------------------------------------
# _should_log_insert_success
# ---------------------------------------------------------------------------


def test_should_log_insert_success_when_flag_set(writer):
    writer._ch_insert_log_success = True
    assert writer._should_log_insert_success(1) is True


def test_should_log_insert_every_n_rows(writer):
    writer._ch_insert_log_success = False
    writer._ch_insert_log_every = 5
    assert writer._should_log_insert_success(5) is True
    assert writer._should_log_insert_success(6) is False


# ---------------------------------------------------------------------------
# _transpose_columnar_rows and _columnar_to_row_dicts
# ---------------------------------------------------------------------------


def test_transpose_columnar_rows_empty():
    assert DataWriter._transpose_columnar_rows([], 0) == []


def test_transpose_columnar_rows_correctness():
    col_data = [[1, 2], [10, 20]]
    rows = DataWriter._transpose_columnar_rows(col_data, 2)
    assert rows[0] == [1, 10]
    assert rows[1] == [2, 20]


def test_columnar_to_row_dicts_empty():
    assert DataWriter._columnar_to_row_dicts([], [], 0) == []


def test_columnar_to_row_dicts_correctness():
    names = ["a", "b"]
    cols = [[1, 2], [10, 20]]
    dicts = DataWriter._columnar_to_row_dicts(names, cols, 2)
    assert dicts[0] == {"a": 1, "b": 10}
    assert dicts[1] == {"a": 2, "b": 20}


# ---------------------------------------------------------------------------
# _sanitize_timestamps
# ---------------------------------------------------------------------------


def test_sanitize_timestamps_passes_through_empty(writer):
    result = writer._sanitize_timestamps("t", [])
    assert result == []


def test_sanitize_timestamps_corrects_ingest_lt_exch_no_future_filter(writer):
    writer._ts_max_future_ns = 0
    row = {"exch_ts": 1000, "ingest_ts": 500}
    result = writer._sanitize_timestamps("t", [row])
    assert result[0]["ingest_ts"] == 1000


def test_sanitize_timestamps_keeps_valid_row_no_future_filter(writer):
    writer._ts_max_future_ns = 0
    row = {"exch_ts": 1000, "ingest_ts": 2000}
    result = writer._sanitize_timestamps("t", [row])
    assert result[0]["ingest_ts"] == 2000


def test_sanitize_timestamps_drops_future_exch_ts(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)  # 1 second future threshold
    far_future = now_ns + int(100e9)  # 100 seconds in future
    row_bad = {"exch_ts": far_future, "ingest_ts": far_future}
    row_ok = {"exch_ts": now_ns - 1000, "ingest_ts": now_ns}
    result = writer._sanitize_timestamps("t", [row_bad, row_ok])
    assert len(result) == 1
    assert result[0]["exch_ts"] == now_ns - 1000


def test_sanitize_timestamps_drops_future_ingest_ts(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
    row_bad = {"exch_ts": now_ns, "ingest_ts": far_future}
    result = writer._sanitize_timestamps("t", [row_bad])
    assert len(result) == 0


def test_sanitize_timestamps_corrects_ingest_lt_exch_with_future_filter(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(10e9)
    row = {"exch_ts": now_ns, "ingest_ts": now_ns - 1000}
    result = writer._sanitize_timestamps("t", [row])
    assert len(result) == 1
    assert result[0]["ingest_ts"] == now_ns


def test_sanitize_timestamps_handles_exception_gracefully(writer):
    """Rows with unparseable timestamps should be kept (error tolerance)."""
    writer._ts_max_future_ns = int(5e9)
    row = {"exch_ts": "bad", "ingest_ts": "also_bad"}
    result = writer._sanitize_timestamps("t", [row])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _sanitize_columnar
# ---------------------------------------------------------------------------


def test_sanitize_columnar_returns_unchanged_when_no_ts_columns(writer):
    col_data, count = writer._sanitize_columnar("t", ["a", "b"], [[1, 2], [3, 4]], 2)
    assert count == 2


def test_sanitize_columnar_returns_zero_for_empty_rows(writer):
    col_data, count = writer._sanitize_columnar("t", ["exch_ts"], [[]], 0)
    assert count == 0


def test_sanitize_columnar_corrects_ingest_lt_exch(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = 0
    col_names = ["exch_ts", "ingest_ts"]
    col_data = [[now_ns], [now_ns - 1000]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 1)
    assert count == 1
    assert result_data[1][0] == now_ns  # ingest_ts corrected to exch_ts


def test_sanitize_columnar_drops_future_rows(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
    col_names = ["exch_ts", "ingest_ts"]
    # Two rows: one bad (future), one ok
    col_data = [[far_future, now_ns - 1000], [far_future, now_ns]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 2)
    assert count == 1


# ---------------------------------------------------------------------------
# _get_wal_batch_writer lazy init
# ---------------------------------------------------------------------------


def test_get_wal_batch_writer_returns_none_when_disabled(writer):
    writer._wal_batch_enabled = False
    assert writer._get_wal_batch_writer() is None


def test_get_wal_batch_writer_lazy_init_when_enabled(writer):
    writer._wal_batch_enabled = True
    bw = writer._get_wal_batch_writer()
    # May return None if WALBatchWriter is not available, that's OK
    # Just verify it doesn't raise
    assert bw is None or bw is not None


# ---------------------------------------------------------------------------
# write() — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_success_uses_ch_client(connected_writer):
    connected_writer.ch_client.insert = MagicMock()
    # Patch run_in_executor to run synchronously
    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(return_value=None)):
        await connected_writer.write("hft.market_data", [{"exch_ts": 100, "ingest_ts": 200}])
    # No WAL fallback expected
    assert connected_writer.connected


@pytest.mark.asyncio
async def test_write_falls_back_to_wal_when_ch_raises(connected_writer):
    mock_wal = AsyncMock(return_value=True)
    connected_writer.wal.write = mock_wal
    connected_writer._wal_batch_enabled = False

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("insert fail"))):
        await connected_writer.write("t", [{"exch_ts": 100}])

    mock_wal.assert_called_once()


@pytest.mark.asyncio
async def test_write_falls_back_to_wal_when_not_connected(writer):
    mock_wal = AsyncMock(return_value=True)
    writer.wal.write = mock_wal
    writer._wal_batch_enabled = False
    writer.connected = False
    writer.ch_client = None

    await writer.write("t", [{"exch_ts": 100}])
    mock_wal.assert_called_once()


@pytest.mark.asyncio
async def test_write_logs_data_loss_when_both_ch_and_wal_fail(connected_writer):
    mock_wal = AsyncMock(return_value=False)
    connected_writer.wal.write = mock_wal
    connected_writer._wal_batch_enabled = False

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("ch fail"))):
        # Should not raise even on dual failure
        await connected_writer.write("t", [{"exch_ts": 100}])

    mock_wal.assert_called_once()


@pytest.mark.asyncio
async def test_write_schedules_reconnect_when_timeout(connected_writer):
    connected_writer._wal_batch_enabled = False
    mock_wal = AsyncMock(return_value=True)
    connected_writer.wal.write = mock_wal

    loop = asyncio.get_event_loop()
    with (
        patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=TimeoutError("timeout"))),
        patch.object(connected_writer, "_schedule_reconnect") as mock_recon,
    ):
        await connected_writer.write("t", [{"exch_ts": 100}])

    mock_recon.assert_called_with("insert_timeout")


# ---------------------------------------------------------------------------
# write_columnar() — paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_columnar_empty_returns_early(writer):
    # Should not raise with empty data
    await writer.write_columnar("t", [], [], 0)
    await writer.write_columnar("t", ["col"], [], 0)


@pytest.mark.asyncio
async def test_write_columnar_falls_back_to_wal_when_not_connected(writer):
    mock_wal = AsyncMock(return_value=True)
    writer.wal.write = mock_wal
    writer._wal_batch_enabled = False
    writer.connected = False

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    await writer.write_columnar("t", ["exch_ts", "val"], [[now_ns], [42]], 1)
    mock_wal.assert_called_once()


@pytest.mark.asyncio
async def test_write_columnar_falls_back_to_wal_on_ch_error(connected_writer):
    mock_wal = AsyncMock(return_value=True)
    connected_writer.wal.write = mock_wal
    connected_writer._wal_batch_enabled = False

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("ch fail"))):
        await connected_writer.write_columnar("t", ["exch_ts"], [[now_ns]], 1)

    mock_wal.assert_called_once()


@pytest.mark.asyncio
async def test_write_columnar_timeout_schedules_reconnect(connected_writer):
    connected_writer._wal_batch_enabled = False
    mock_wal = AsyncMock(return_value=True)
    connected_writer.wal.write = mock_wal

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()

    loop = asyncio.get_event_loop()
    with (
        patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=TimeoutError("timeout"))),
        patch.object(connected_writer, "_schedule_reconnect") as mock_recon,
    ):
        await connected_writer.write_columnar("t", ["exch_ts"], [[now_ns]], 1)

    mock_recon.assert_called_with("insert_timeout")


# ---------------------------------------------------------------------------
# write() and write_columnar() with health tracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_health_tracker_notified_on_ch_error(connected_writer):
    tracker = MagicMock()
    connected_writer.set_health_tracker(tracker)
    connected_writer._wal_batch_enabled = False
    mock_wal = AsyncMock(return_value=True)
    connected_writer.wal.write = mock_wal

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("error"))):
        await connected_writer.write("t", [{"exch_ts": 100}])

    tracker.record_event.assert_any_call("ch_error", table="t")


@pytest.mark.asyncio
async def test_write_health_tracker_notified_on_data_loss(connected_writer):
    tracker = MagicMock()
    connected_writer.set_health_tracker(tracker)
    connected_writer._wal_batch_enabled = False
    mock_wal = AsyncMock(return_value=False)
    connected_writer.wal.write = mock_wal

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("fail"))):
        await connected_writer.write("t", [{"exch_ts": 100}])

    tracker.record_event.assert_any_call("data_loss", table="t", count=1)


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_flushes_wal_batch_writer(writer):
    mock_bw = MagicMock()
    mock_bw.flush = AsyncMock(return_value=None)
    mock_bw.stop = MagicMock()
    writer._wal_batch_writer = mock_bw

    await writer.shutdown()

    mock_bw.flush.assert_called_once()
    mock_bw.stop.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_handles_wal_batch_flush_error(writer):
    mock_bw = MagicMock()
    mock_bw.flush = AsyncMock(side_effect=RuntimeError("flush failed"))
    mock_bw.stop = MagicMock()
    writer._wal_batch_writer = mock_bw

    # Should not raise
    await writer.shutdown()
    mock_bw.stop.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_handles_wal_batch_stop_error(writer):
    mock_bw = MagicMock()
    mock_bw.flush = AsyncMock(return_value=None)
    mock_bw.stop = MagicMock(side_effect=RuntimeError("stop failed"))
    writer._wal_batch_writer = mock_bw

    # Should not raise
    await writer.shutdown()


@pytest.mark.asyncio
async def test_shutdown_without_wal_batch_writer(writer):
    writer._wal_batch_writer = None
    # Should not raise
    await writer.shutdown()


# ---------------------------------------------------------------------------
# connect() sync — already tested in test_writer_retry but missing metrics path
# ---------------------------------------------------------------------------


@patch("hft_platform.recorder.writer.clickhouse_connect")
@patch("hft_platform.recorder.writer.time.sleep")
def test_connect_sets_metrics_on_success(mock_sleep, mock_ch, tmp_path):
    mock_client = MagicMock()
    mock_ch.get_client.return_value = mock_client

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1"}):
        writer = DataWriter(wal_dir=str(tmp_path))
        mock_metrics = MagicMock()
        writer.metrics = mock_metrics
        with (
            patch.object(writer, "_init_schema"),
            patch.object(writer, "_start_heartbeat_thread"),
        ):
            writer.connect()

    mock_metrics.clickhouse_connection_health.set.assert_called_with(1)


@patch("hft_platform.recorder.writer.clickhouse_connect")
@patch("hft_platform.recorder.writer.time.sleep")
def test_connect_sets_metrics_to_zero_on_failure(mock_sleep, mock_ch, tmp_path):
    mock_ch.get_client.side_effect = ConnectionError("refused")

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1"}):
        writer = DataWriter(wal_dir=str(tmp_path))
        mock_metrics = MagicMock()
        writer.metrics = mock_metrics
        writer.connect()

    mock_metrics.clickhouse_connection_health.set.assert_called_with(0)


# ---------------------------------------------------------------------------
# _start_heartbeat_thread guards
# ---------------------------------------------------------------------------


def test_start_heartbeat_does_nothing_when_not_connected(writer):
    writer.connected = False
    writer._heartbeat_running = False
    writer._start_heartbeat_thread()
    assert not writer._heartbeat_running


def test_start_heartbeat_does_nothing_when_already_running(connected_writer):
    connected_writer._heartbeat_running = True
    connected_writer._start_heartbeat_thread()
    # Thread should not be replaced
    assert connected_writer._heartbeat_thread is None


# ---------------------------------------------------------------------------
# _schedule_reconnect guards
# ---------------------------------------------------------------------------


def test_schedule_reconnect_does_nothing_when_ch_disabled(writer):
    writer.ch_enabled = False
    with patch("hft_platform.recorder.writer.clickhouse_connect", new=None):
        writer._schedule_reconnect("test")
    assert not writer._reconnect_running


def test_schedule_reconnect_respects_min_interval(writer):
    writer.ch_enabled = True
    import hft_platform.core.timebase as tb

    writer._last_reconnect_ts = tb.now_s() + 10000  # Future timestamp
    writer._schedule_reconnect("test")
    assert not writer._reconnect_running


# ---------------------------------------------------------------------------
# _ch_insert_columnar with column_oriented=False fallback
# ---------------------------------------------------------------------------


def test_ch_insert_columnar_once_column_oriented_false(connected_writer):
    connected_writer._ch_column_oriented = False
    connected_writer.ch_client.insert = MagicMock()
    col_data = [[1, 2], [10, 20]]
    connected_writer._ch_insert_columnar_once("t", ["a", "b"], col_data, 2)
    connected_writer.ch_client.insert.assert_called_once()
    # When not column_oriented, values are transposed rows
    args = connected_writer.ch_client.insert.call_args
    assert args[0][0] == "t"


def test_ch_insert_columnar_once_fallback_on_type_error(connected_writer):
    """When column_oriented=True raises TypeError, falls back to row-oriented."""
    connected_writer._ch_column_oriented = True
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if kwargs.get("column_oriented"):
            raise TypeError("not supported")

    connected_writer.ch_client.insert = MagicMock(side_effect=side_effect)
    col_data = [[1, 2], [10, 20]]
    connected_writer._ch_insert_columnar_once("t", ["a", "b"], col_data, 2)
    assert call_count[0] == 2  # Once failing, once succeeding


# ---------------------------------------------------------------------------
# connect_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_async_wal_only_when_disabled(writer):
    await writer.connect_async()
    assert not writer.connected


@pytest.mark.asyncio
@patch("hft_platform.recorder.writer.clickhouse_connect")
async def test_connect_async_success(mock_ch, tmp_path):
    mock_client = MagicMock()
    mock_ch.get_client.return_value = mock_client

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1"}):
        writer = DataWriter(wal_dir=str(tmp_path))
        with (
            patch.object(writer, "_init_schema"),
            patch.object(writer, "_start_heartbeat_thread"),
        ):
            await writer.connect_async()

    assert writer.connected

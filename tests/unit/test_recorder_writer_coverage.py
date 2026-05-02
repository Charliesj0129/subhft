"""Coverage-focused tests for recorder/writer.py.

Targets uncovered paths: env-var credential resolution, interface fallback,
schema init failure, write/write_columnar success and error paths,
_sanitize_timestamps, _sanitize_columnar, heartbeat, shutdown, get_status.
"""

import asyncio
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

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
    writer.ch_params.pop("interface", None)
    exc = Exception("unrecognized client type native")
    result = writer._maybe_fallback_clickhouse_interface(exc)
    assert result is False


def test_fallback_interface_port_stays_when_not_canonical(writer):
    writer.ch_params["interface"] = "native"
    writer.ch_params["port"] = 19000
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
    assert writer._schema_initialized
    assert writer.connected


def test_init_schema_sets_metrics_on_failure(writer):
    writer.connected = True
    writer.ch_client = MagicMock()
    mock_metrics = MagicMock()
    writer.metrics = mock_metrics
    with patch("hft_platform.recorder.writer.apply_schema", side_effect=RuntimeError("fail")):
        writer._init_schema()
    mock_metrics.recorder_schema_init_failed.set.assert_called_with(1)


def test_init_schema_sets_metrics_on_success(writer):
    writer.connected = True
    writer.ch_client = MagicMock()
    mock_metrics = MagicMock()
    writer.metrics = mock_metrics
    with patch("hft_platform.recorder.writer.apply_schema"):
        writer._init_schema()
    mock_metrics.recorder_schema_init_failed.set.assert_called_with(0)


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
# _compute_backoff_delay
# ---------------------------------------------------------------------------


def test_compute_backoff_delay_bounded(writer):
    for attempt in range(10):
        delay = writer._compute_backoff_delay(attempt)
        assert delay >= 0.1
        assert delay <= writer._max_backoff_s * (1 + writer._jitter_factor) + 0.01


def test_compute_backoff_delay_increases_with_attempt(writer):
    """Higher attempt numbers produce larger base delays (before jitter)."""
    delays_0 = [writer._compute_backoff_delay(0) for _ in range(20)]
    delays_5 = [writer._compute_backoff_delay(5) for _ in range(20)]
    avg_0 = sum(delays_0) / len(delays_0)
    avg_5 = sum(delays_5) / len(delays_5)
    assert avg_5 >= avg_0


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


def test_iter_row_chunks_no_split_when_zero(writer):
    writer._ch_insert_chunk_rows = 0
    data = [1, 2, 3]
    chunks = writer._iter_row_chunks(data)
    assert len(chunks) == 1
    assert chunks[0] == [1, 2, 3]


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


def test_should_log_insert_returns_false_when_both_disabled(writer):
    writer._ch_insert_log_success = False
    writer._ch_insert_log_every = 0
    assert writer._should_log_insert_success(1) is False


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


def test_columnar_to_row_dicts_empty_names():
    assert DataWriter._columnar_to_row_dicts([], [[1]], 1) == []


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
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
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
    writer._ts_max_future_ns = int(5e9)
    row = {"exch_ts": "bad", "ingest_ts": "also_bad"}
    result = writer._sanitize_timestamps("t", [row])
    assert len(result) == 1


def test_sanitize_timestamps_no_future_filter_exception_path(writer):
    """Exception in no-future-filter path still appends the row."""
    writer._ts_max_future_ns = 0
    row = {"exch_ts": object(), "ingest_ts": object()}
    result = writer._sanitize_timestamps("t", [row])
    assert len(result) == 1


def test_sanitize_timestamps_row_without_ts_fields(writer):
    """Rows without exch_ts or ingest_ts pass through unchanged."""
    writer._ts_max_future_ns = int(5e9)
    row = {"symbol": "2330", "price": 100}
    result = writer._sanitize_timestamps("t", [row])
    assert len(result) == 1
    assert result[0]["symbol"] == "2330"


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
    assert result_data[1][0] == now_ns


def test_sanitize_columnar_drops_future_rows(writer):
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
    col_names = ["exch_ts", "ingest_ts"]
    col_data = [[far_future, now_ns - 1000], [far_future, now_ns]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 2)
    assert count == 1


def test_sanitize_columnar_drops_future_ingest_only(writer):
    """Drop row when ingest_ts is far future but exch_ts is fine."""
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
    col_names = ["exch_ts", "ingest_ts"]
    col_data = [[now_ns], [far_future]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 1)
    assert count == 0


def test_sanitize_columnar_exch_only_column(writer):
    """Sanitize works with only exch_ts column (no ingest_ts)."""
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    col_names = ["exch_ts", "value"]
    col_data = [[now_ns], [42]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 1)
    assert count == 1


def test_sanitize_columnar_all_rows_dropped_returns_empty(writer):
    """When all rows are dropped, returns empty column_data."""
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    far_future = now_ns + int(100e9)
    col_names = ["exch_ts", "ingest_ts"]
    col_data = [[far_future], [far_future]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 1)
    assert count == 0
    assert result_data == []


def test_sanitize_columnar_exception_in_row_keeps_row(writer):
    """Exception parsing a row still keeps it (tolerance)."""
    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()
    writer._ts_max_future_ns = int(1e9)
    col_names = ["exch_ts", "ingest_ts"]
    col_data = [["bad_value"], ["bad_value"]]
    result_data, count = writer._sanitize_columnar("t", col_names, col_data, 1)
    # Exception path keeps the row
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
    assert writer._get_wal_batch_writer() is bw


def test_get_wal_batch_writer_returns_none_on_import_error(writer):
    """If WALBatchWriter import fails, returns None."""
    writer._wal_batch_enabled = True
    with patch("hft_platform.recorder.writer.DataWriter._get_wal_batch_writer") as mock:
        mock.return_value = None
        result = mock()
    assert result is None


# ---------------------------------------------------------------------------
# write() paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_success_uses_ch_client(connected_writer):
    connected_writer.ch_client.insert = MagicMock()
    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(return_value=None)):
        await connected_writer.write("hft.market_data", [{"exch_ts": 100, "ingest_ts": 200}])
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
async def test_write_uses_batch_writer_when_available(connected_writer):
    """Write falls back to WAL batch writer (not plain WAL) when batch writer available."""
    mock_bw = MagicMock()
    mock_bw.add = AsyncMock(return_value=True)
    connected_writer._wal_batch_writer = mock_bw
    connected_writer._wal_batch_enabled = True

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("fail"))):
        await connected_writer.write("t", [{"exch_ts": 100}])

    mock_bw.add.assert_called_once()


@pytest.mark.asyncio
async def test_write_logs_data_loss_when_both_ch_and_wal_fail(connected_writer):
    from hft_platform.recorder.writer import WriterDoubleFaultError

    mock_wal = AsyncMock(return_value=False)
    connected_writer.wal.write = mock_wal
    connected_writer._wal_batch_enabled = False

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("ch fail"))):
        with pytest.raises(WriterDoubleFaultError):
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


@pytest.mark.asyncio
async def test_write_schedules_reconnect_when_not_connected_and_ch_enabled(writer):
    """When ch_enabled but not connected, _schedule_reconnect is called."""
    writer.ch_enabled = True
    writer.connected = False
    writer.ch_client = None
    writer._wal_batch_enabled = False
    mock_wal = AsyncMock(return_value=True)
    writer.wal.write = mock_wal

    with patch.object(writer, "_schedule_reconnect") as mock_recon:
        await writer.write("t", [{"exch_ts": 100}])
    mock_recon.assert_called_with("not_connected")


@pytest.mark.asyncio
async def test_write_empty_data_returns_early(writer):
    """write() with empty data returns immediately."""
    writer.wal.write = AsyncMock(return_value=True)
    await writer.write("t", [])
    writer.wal.write.assert_not_called()


# ---------------------------------------------------------------------------
# write_columnar() paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_columnar_empty_returns_early(writer):
    await writer.write_columnar("t", [], [], 0)
    await writer.write_columnar("t", ["col"], [], 0)
    assert not writer.connected


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
async def test_write_columnar_uses_batch_writer_add_columnar(connected_writer):
    """Columnar WAL fallback uses batch writer add_columnar when available."""
    mock_bw = MagicMock()
    mock_bw.add_columnar = AsyncMock(return_value=True)
    connected_writer._wal_batch_writer = mock_bw
    connected_writer._wal_batch_enabled = True

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("fail"))):
        await connected_writer.write_columnar("t", ["exch_ts"], [[now_ns]], 1)

    mock_bw.add_columnar.assert_called_once()


@pytest.mark.asyncio
async def test_write_columnar_batch_writer_without_add_columnar(connected_writer):
    """Batch writer without add_columnar falls back to add() with row dicts."""
    mock_bw = MagicMock(spec=["add"])
    mock_bw.add = AsyncMock(return_value=True)
    connected_writer._wal_batch_writer = mock_bw
    connected_writer._wal_batch_enabled = True

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("fail"))):
        await connected_writer.write_columnar("t", ["exch_ts"], [[now_ns]], 1)

    mock_bw.add.assert_called_once()


@pytest.mark.asyncio
async def test_write_columnar_double_fault_raises(connected_writer):
    """WriterDoubleFaultError raised when both CH and WAL fail for columnar."""
    from hft_platform.recorder.writer import WriterDoubleFaultError

    mock_wal = AsyncMock(return_value=False)
    connected_writer.wal.write = mock_wal
    connected_writer._wal_batch_enabled = False

    import hft_platform.core.timebase as tb

    now_ns = tb.now_ns()

    loop = asyncio.get_event_loop()
    with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=RuntimeError("fail"))):
        with pytest.raises(WriterDoubleFaultError):
            await connected_writer.write_columnar("t", ["exch_ts"], [[now_ns]], 1)


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

    await writer.shutdown()
    mock_bw.stop.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_handles_wal_batch_stop_error(writer):
    mock_bw = MagicMock()
    mock_bw.flush = AsyncMock(return_value=None)
    mock_bw.stop = MagicMock(side_effect=RuntimeError("stop failed"))
    writer._wal_batch_writer = mock_bw

    await writer.shutdown()
    mock_bw.flush.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_without_wal_batch_writer(writer):
    writer._wal_batch_writer = None
    await writer.shutdown()
    assert writer._wal_batch_writer is None


# ---------------------------------------------------------------------------
# connect() sync
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


@patch("hft_platform.recorder.writer.clickhouse_connect")
@patch("hft_platform.recorder.writer.time.sleep")
def test_connect_retries_with_backoff(mock_sleep, mock_ch, tmp_path):
    """Connect retries on failure then succeeds."""
    mock_client = MagicMock()
    mock_ch.get_client.side_effect = [ConnectionError("fail"), mock_client]

    with patch.dict(
        "os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "2", "HFT_CH_BASE_DELAY_S": "0.01"}
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
        with (
            patch.object(writer, "_init_schema"),
            patch.object(writer, "_start_heartbeat_thread"),
        ):
            writer.connect()
    assert writer.connected


@patch("hft_platform.recorder.writer.clickhouse_connect")
@patch("hft_platform.recorder.writer.time.sleep")
def test_connect_with_native_fallback(mock_sleep, mock_ch, tmp_path):
    """Connect falls back to HTTP when native interface not supported."""
    mock_client = MagicMock()
    mock_ch.get_client.side_effect = [Exception("unrecognized client type native"), mock_client]

    with patch.dict(
        "os.environ",
        {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1", "HFT_CLICKHOUSE_PORT": "9000"},
        clear=False,
    ):
        writer = DataWriter(wal_dir=str(tmp_path))
        with (
            patch.object(writer, "_init_schema"),
            patch.object(writer, "_start_heartbeat_thread"),
        ):
            writer.connect()
    assert writer.connected


def test_connect_wal_only_when_disabled(writer):
    """connect() in WAL-only mode does nothing."""
    writer.connect()
    assert not writer.connected


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


@pytest.mark.asyncio
@patch("hft_platform.recorder.writer.clickhouse_connect")
async def test_connect_async_failure_sets_metrics(mock_ch, tmp_path):
    mock_ch.get_client.side_effect = ConnectionError("refused")

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1"}):
        writer = DataWriter(wal_dir=str(tmp_path))
        mock_metrics = MagicMock()
        writer.metrics = mock_metrics
        await writer.connect_async()

    assert not writer.connected
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

    writer._last_reconnect_ts = tb.now_s() + 10000
    writer._schedule_reconnect("test")
    assert not writer._reconnect_running


def test_schedule_reconnect_skips_when_already_running(writer):
    writer.ch_enabled = True
    writer._reconnect_running = True
    writer._schedule_reconnect("test")
    assert writer._reconnect_running


# ---------------------------------------------------------------------------
# _ch_insert_columnar with column_oriented fallbacks
# ---------------------------------------------------------------------------


def test_ch_insert_columnar_once_column_oriented_false(connected_writer):
    connected_writer._ch_column_oriented = False
    connected_writer.ch_client.insert = MagicMock()
    col_data = [[1, 2], [10, 20]]
    connected_writer._ch_insert_columnar_once("t", ["a", "b"], col_data, 2)
    connected_writer.ch_client.insert.assert_called_once()
    args = connected_writer.ch_client.insert.call_args
    assert args[0][0] == "t"


def test_ch_insert_columnar_once_fallback_on_type_error(connected_writer):
    connected_writer._ch_column_oriented = True
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if kwargs.get("column_oriented"):
            raise TypeError("not supported")

    connected_writer.ch_client.insert = MagicMock(side_effect=side_effect)
    col_data = [[1, 2], [10, 20]]
    connected_writer._ch_insert_columnar_once("t", ["a", "b"], col_data, 2)
    assert call_count[0] == 2


def test_ch_insert_columnar_once_slow_insert_warning(connected_writer):
    """Slow insert triggers warning log (elapsed > warn threshold)."""
    connected_writer._ch_column_oriented = True
    connected_writer._insert_warn_ms = 0  # any insert is "slow"
    connected_writer.ch_client.insert = MagicMock()
    col_data = [[1], [10]]
    connected_writer._ch_insert_columnar_once("t", ["a", "b"], col_data, 1)
    assert connected_writer.ch_client.insert.called


def test_ch_insert_columnar_empty_data_returns_early(connected_writer):
    """_ch_insert_columnar with empty data returns without insert."""
    connected_writer._ch_insert_columnar("t", ["a"], [], 0)
    connected_writer.ch_client.insert.assert_not_called()


def test_ch_insert_columnar_disconnected_raises(connected_writer):
    """Insert raises ConnectionError when client is None mid-insert."""
    connected_writer.ch_client = None
    with pytest.raises(ConnectionError):
        connected_writer._ch_insert_columnar_once("t", ["a"], [[1]], 1)


# ---------------------------------------------------------------------------
# _ch_insert (row dict path)
# ---------------------------------------------------------------------------


def test_ch_insert_empty_data(connected_writer):
    """_ch_insert with empty data returns immediately."""
    connected_writer._ch_insert("t", [])
    connected_writer.ch_client.insert.assert_not_called()


def test_ch_insert_once_slow_warning(connected_writer):
    """Slow row insert triggers warning log."""
    connected_writer._insert_warn_ms = 0
    connected_writer.ch_client.insert = MagicMock()
    connected_writer._ch_insert_once("t", [{"a": 1, "b": 2}])
    assert connected_writer.ch_client.insert.called


def test_ch_insert_once_disconnected_raises(connected_writer):
    """_ch_insert_once raises ConnectionError when client disconnects."""
    connected_writer.ch_client = None
    with pytest.raises(ConnectionError):
        connected_writer._ch_insert_once("t", [{"a": 1}])


# ---------------------------------------------------------------------------
# _is_native_interface_unsupported_error
# ---------------------------------------------------------------------------


def test_is_native_interface_unsupported_error_matches():
    exc = Exception("unrecognized client type native")
    assert DataWriter._is_native_interface_unsupported_error(exc) is True


def test_is_native_interface_unsupported_error_no_match():
    exc = Exception("connection refused")
    assert DataWriter._is_native_interface_unsupported_error(exc) is False


def test_is_native_interface_invalid_interface_match():
    exc = Exception("invalid interface")
    assert DataWriter._is_native_interface_unsupported_error(exc) is True


# ---------------------------------------------------------------------------
# _start_heartbeat_thread: actually runs the loop
# ---------------------------------------------------------------------------


def test_start_heartbeat_runs_and_stops_on_heartbeat_failure(tmp_path):
    """Heartbeat loop detects failure and marks connection stale."""

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_HEARTBEAT_INTERVAL_S": "0.01"}):
        w = DataWriter(wal_dir=str(tmp_path))
    w.connected = True
    w.ch_enabled = True
    w.ch_client = MagicMock()
    w.ch_client.command.side_effect = RuntimeError("heartbeat fail")
    mock_metrics = MagicMock()
    w.metrics = mock_metrics

    with patch.object(w, "_schedule_reconnect"):
        w._start_heartbeat_thread()
        # Wait briefly for heartbeat to detect failure
        import time

        time.sleep(0.1)

    # Heartbeat should have marked connection as stale
    assert w.connected is False
    assert w.ch_client is None
    assert w._heartbeat_running is False
    mock_metrics.clickhouse_connection_health.set.assert_called_with(0)


def test_start_heartbeat_stops_when_flag_cleared(tmp_path):
    """Heartbeat loop stops when _heartbeat_running is set to False."""
    import time

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_HEARTBEAT_INTERVAL_S": "0.01"}):
        w = DataWriter(wal_dir=str(tmp_path))
    w.connected = True
    w.ch_enabled = True
    w.ch_client = MagicMock()
    w.ch_client.command.return_value = None  # heartbeat succeeds

    w._start_heartbeat_thread()
    assert w._heartbeat_running is True

    # Stop it
    w._heartbeat_running = False
    time.sleep(0.05)
    assert w._heartbeat_running is False


# ---------------------------------------------------------------------------
# _schedule_reconnect: actually runs reconnect thread
# ---------------------------------------------------------------------------


def test_schedule_reconnect_runs_reconnect_thread(tmp_path):
    """_schedule_reconnect spawns a thread that calls connect()."""
    import time

    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1", "HFT_CH_MAX_RETRIES": "1"}):
        w = DataWriter(wal_dir=str(tmp_path))
    w.ch_enabled = True
    w._last_reconnect_ts = 0
    w._reconnect_running = False

    with (
        patch("hft_platform.recorder.writer.clickhouse_connect", MagicMock()),
        patch.object(w, "connect") as mock_connect,
    ):
        w._schedule_reconnect("test_reason")
        time.sleep(0.1)

    mock_connect.assert_called_once()
    assert w._reconnect_running is False


def test_schedule_reconnect_skips_when_lock_held(tmp_path):
    """_schedule_reconnect does nothing when reconnect lock is already held."""
    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1"}):
        w = DataWriter(wal_dir=str(tmp_path))
    w.ch_enabled = True
    w._last_reconnect_ts = 0
    w._reconnect_running = False
    # Hold the lock
    w._reconnect_lock.acquire()
    try:
        w._schedule_reconnect("test")
        assert not w._reconnect_running
    finally:
        w._reconnect_lock.release()


# ---------------------------------------------------------------------------
# shutdown: semaphore drain timeout path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drains_semaphore_before_pool_shutdown(writer):
    """shutdown waits for semaphore drain and shuts down executor."""
    writer._wal_batch_writer = None
    await writer.shutdown()
    # Executor should be shut down
    assert writer._executor._shutdown


@pytest.mark.asyncio
async def test_shutdown_timeout_on_in_flight_inserts(tmp_path):
    """shutdown times out when semaphore is partially acquired."""
    with patch.dict(
        "os.environ",
        {
            "HFT_CLICKHOUSE_ENABLED": "0",
            "HFT_CH_SHUTDOWN_TIMEOUT_S": "0",
        },
    ):
        w = DataWriter(wal_dir=str(tmp_path))
    w._wal_batch_writer = None

    # Acquire the semaphore to simulate in-flight inserts
    await w._insert_semaphore.acquire()

    # Patch time.monotonic to make deadline already passed
    import time as _time

    original_monotonic = _time.monotonic
    call_count = [0]

    def fast_monotonic():
        call_count[0] += 1
        # First call sets deadline; subsequent calls exceed it
        if call_count[0] <= 1:
            return original_monotonic()
        return original_monotonic() + 100  # way past deadline

    with patch("hft_platform.recorder.writer.time.monotonic", side_effect=fast_monotonic):
        await w.shutdown()

    # Executor should shut down (with wait=False due to timeout)
    assert w._executor._shutdown


# ---------------------------------------------------------------------------
# _create_clickhouse_client with fallback
# ---------------------------------------------------------------------------


@patch("hft_platform.recorder.writer.clickhouse_connect")
def test_create_clickhouse_client_with_fallback(mock_ch, writer):
    """_create_clickhouse_client retries with fallback on native interface error."""
    mock_client = MagicMock()
    mock_ch.get_client.side_effect = [Exception("unrecognized client type native"), mock_client]
    writer.ch_params["interface"] = "native"
    writer.ch_params["port"] = 9000

    result = writer._create_clickhouse_client()
    assert result is mock_client
    assert writer._native_interface_fallback_used is True


@patch("hft_platform.recorder.writer.clickhouse_connect")
def test_create_clickhouse_client_raises_non_native_error(mock_ch, writer):
    """_create_clickhouse_client raises non-native interface errors."""
    mock_ch.get_client.side_effect = ConnectionError("refused")
    with pytest.raises(ConnectionError):
        writer._create_clickhouse_client()

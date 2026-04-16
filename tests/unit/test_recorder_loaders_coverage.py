"""Coverage tests for recorder loader modules.

Targets uncovered paths in:
  - _loader_batch.py: format_* edge cases, insert_batch_for_table
  - _loader_wal.py: process_single_file edge cases, process_files parallel
  - _loader_dlq.py: replay_dlq exception path, cleanup with metrics exceptions
  - _loader_ch.py: connect error paths, insert_with_retry, dedup helpers
  - schema.py: apply_schema edge cases, _init_migrations_table
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder._loader_batch import (
    format_fills,
    format_latency_spans,
    format_market_data,
    format_orders,
    format_pnl_snapshots,
    format_risk_log,
    format_trades,
    insert_batch_for_table,
)
from hft_platform.recorder._loader_ch import (
    compute_connect_backoff,
    compute_insert_backoff,
    connect,
    insert_with_dedup,
    insert_with_retry,
    is_duplicate,
    record_dedup,
)
from hft_platform.recorder._loader_dlq import (
    check_wal_accumulation,
    cleanup_old_archive_files,
    cleanup_old_corrupt_files,
    cleanup_old_dlq_files,
    replay_dlq,
)
from hft_platform.recorder._loader_wal import (
    parse_batch_table_name,
    parse_table_from_filename,
    process_files,
    process_single_file,
    save_manifest,
)
from hft_platform.recorder.schema import (
    _execute_all,
    _view_uses_legacy_price,
    apply_schema,
    ensure_price_scaled_views,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_svc(tmp_path) -> SimpleNamespace:
    wal_dir = str(tmp_path / "wal")
    dlq_dir = str(tmp_path / "dlq")
    archive_dir = str(tmp_path / "archive")
    corrupt_dir = str(tmp_path / "corrupt")
    os.makedirs(wal_dir, exist_ok=True)
    os.makedirs(dlq_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    return SimpleNamespace(
        wal_dir=wal_dir,
        dlq_dir=dlq_dir,
        archive_dir=archive_dir,
        corrupt_dir=corrupt_dir,
        ch_client=MagicMock(),
        ch_host="localhost",
        ch_port=9000,
        metrics=None,
        _manifest=set(),
        _manifest_path=str(tmp_path / "manifest.txt"),
        _manifest_enabled=True,
        _manifest_lock=threading.Lock(),
        _ch_lock=threading.Lock(),
        _last_dlq_cleanup_ts=0,
        _dlq_cleanup_interval_s=0,
        _dlq_retention_days=1,
        _dlq_archive_path="",
        _last_corrupt_cleanup_ts=0,
        _corrupt_retention_days=1,
        _last_archive_cleanup_ts=0,
        _archive_retention_days=1,
        _last_wal_check_ts=0,
        _wal_check_interval_s=0,
        _wal_size_warning_mb=100,
        _wal_size_critical_mb=500,
        _processed_files_total=0,
        _eta_sample_last_ts=0.0,
        _eta_sample_last_processed=0,
        _strict_order=False,
        _last_processed_ts=0,
        _loader_stats_lock=threading.Lock(),
        _loader_concurrency=1,
        _dedup_enabled=False,
        _insert_max_retries=2,
        _insert_base_delay_s=0.01,
        _insert_max_backoff_s=0.1,
        _connect_base_delay_s=1.0,
        _connect_max_backoff_s=10.0,
        _claim_registry=SimpleNamespace(
            try_claim=MagicMock(return_value=True),
            release_claim=MagicMock(),
        ),
        insert_batch=MagicMock(return_value=True),
        _insert_with_dedup=MagicMock(return_value=True),
        _insert_with_retry=MagicMock(return_value=True),
        _write_to_dlq=MagicMock(),
        _quarantine_corrupt_file=MagicMock(),
        _is_duplicate=MagicMock(return_value=False),
        _record_dedup=MagicMock(),
    )


def _make_wal_file(wal_dir: str, filename: str, content: str, age_s: float = 10) -> str:
    os.makedirs(wal_dir, exist_ok=True)
    fpath = os.path.join(wal_dir, filename)
    with open(fpath, "w") as f:
        f.write(content)
    past = time.time() - age_s
    os.utime(fpath, (past, past))
    return fpath


# ===========================================================================
# _loader_batch.py tests
# ===========================================================================


class TestFormatMarketDataEdgeCases:
    def test_price_from_mid_scaled_bid_ask(self) -> None:
        """Price derived from mid when best_bid/best_ask are scaled ints."""
        rows = [
            {
                "symbol": "TEST",
                "type": "bidask",
                "best_bid": 200000000,
                "best_ask": 201000000,
                "exch_ts": 1000,
            }
        ]
        cols, data = format_market_data(rows)
        assert data[0][5] == (200000000 + 201000000) // 2

    def test_no_price_at_all_defaults_to_zero(self) -> None:
        """When no price source is available, price_scaled defaults to 0."""
        rows = [{"symbol": "TEST", "type": "tick", "exch_ts": 1000}]
        cols, data = format_market_data(rows)
        assert data[0][5] == 0

    def test_bid_ask_from_nested_arrays(self) -> None:
        rows = [
            {
                "symbol": "TEST",
                "type": "bidask",
                "bids": [[100.0, 5]],
                "asks": [[101.0, 3]],
                "exch_ts": 1000,
            }
        ]
        cols, data = format_market_data(rows)
        assert isinstance(data[0][7], list)
        assert len(data[0][7]) == 1

    def test_meta_fallback_for_timestamps(self) -> None:
        rows = [
            {
                "symbol": "TEST",
                "meta": {"source_ts": 500, "local_ts": 600, "topic": "tick"},
                "price_scaled": 100000,
            }
        ]
        cols, data = format_market_data(rows)
        assert data[0][3] == 500

    def test_multi_instrument_fields(self) -> None:
        rows = [
            {
                "symbol": "TXO123",
                "type": "tick",
                "exch_ts": 1000,
                "price_scaled": 100,
                "instrument_type": "option",
                "underlying": "TX",
                "strike_scaled": 180000000,
                "option_right": "call",
                "expiry": "2026-06-20",
            }
        ]
        cols, data = format_market_data(rows)
        assert data[0][13] == "option"
        assert data[0][14] == "TX"
        assert data[0][15] == 180000000
        assert data[0][16] == "call"
        assert data[0][17] == date(2026, 6, 20)


class TestFormatOrdersEdgeCases:
    def test_missing_price_defaults_to_zero(self) -> None:
        rows = [{"order_id": "O1"}]
        cols, data = format_orders(rows)
        assert data[0][4] == 0

    def test_instrument_fields(self) -> None:
        rows = [
            {
                "order_id": "O1",
                "instrument_type": "future",
                "oc_type": "open",
            }
        ]
        cols, data = format_orders(rows)
        assert data[0][9] == "future"
        assert data[0][10] == "open"


class TestFormatTradesEdgeCases:
    def test_fill_id_fallback_for_trade_id(self) -> None:
        rows = [{"fill_id": "F1", "price_scaled": 100, "exch_ts": 1000}]
        cols, data = format_trades(rows)
        assert data[0][0] == "F1"

    def test_action_fallback_for_side(self) -> None:
        rows = [{"action": "SELL", "exch_ts": 1000}]
        cols, data = format_trades(rows)
        assert data[0][4] == "SELL"


class TestFormatFillsEdgeCases:
    def test_ts_fallback_chain(self) -> None:
        rows = [{"ts": 5000, "price_scaled": 100}]
        cols, data = format_fills(rows)
        assert data[0][0] == 5000

    def test_instrument_and_oc_type(self) -> None:
        rows = [
            {
                "ts_exchange": 1000,
                "price_scaled": 100,
                "instrument_type": "future",
                "oc_type": "close",
            }
        ]
        cols, data = format_fills(rows)
        assert data[0][15] == "future"
        assert data[0][16] == "close"


class TestFormatRiskLogEdgeCases:
    def test_timestamp_fallback(self) -> None:
        rows = [{"ingest_ts": 3000, "metric": "pnl", "value": 1.0}]
        cols, data = format_risk_log(rows)
        assert data[0][0] == 3000

    def test_string_context_passed_through(self) -> None:
        rows = [{"ts": 1000, "context": "raw_string"}]
        cols, data = format_risk_log(rows)
        assert data[0][4] == "raw_string"


class TestFormatPnlSnapshotsEdgeCases:
    def test_ts_fallback_to_timestamp(self) -> None:
        rows = [{"timestamp": 7000}]
        cols, data = format_pnl_snapshots(rows)
        assert data[0][0] == 7000

    def test_all_defaults(self) -> None:
        rows = [{}]
        cols, data = format_pnl_snapshots(rows)
        assert data[0][4] == 0  # net_qty
        assert data[0][10] == 0.0  # drawdown_pct


class TestFormatLatencySpansEdgeCases:
    def test_timestamp_fallback(self) -> None:
        rows = [{"timestamp": 8000, "stage": "risk"}]
        cols, data = format_latency_spans(rows)
        assert data[0][0] == 8000

    def test_missing_fields_default(self) -> None:
        rows = [{}]
        cols, data = format_latency_spans(rows)
        assert data[0][1] == ""  # stage
        assert data[0][2] == 0  # latency_us


class TestInsertBatchForTable:
    def test_unknown_table_returns_false(self) -> None:
        svc = MagicMock()
        result = insert_batch_for_table(svc, "nonexistent", [{"a": 1}])
        assert result is False

    def test_known_table_calls_insert_with_retry(self) -> None:
        svc = MagicMock()
        svc._insert_with_retry.return_value = True
        result = insert_batch_for_table(svc, "orders", [{"order_id": "O1"}])
        assert result is True
        svc._insert_with_retry.assert_called_once()

    def test_all_table_keys_supported(self) -> None:
        svc = MagicMock()
        svc._insert_with_retry.return_value = True
        for table in ["market_data", "orders", "trades", "fills", "risk_log",
                       "backtest_runs", "pnl_snapshots", "latency_spans"]:
            result = insert_batch_for_table(svc, table, [{}])
            assert result is True


# ===========================================================================
# _loader_ch.py tests
# ===========================================================================


class TestConnect:
    def test_connect_success(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        mock_client = MagicMock()
        with patch("hft_platform.recorder._loader_ch.get_ch_client", return_value=mock_client):
            with patch("hft_platform.recorder._loader_ch.apply_schema"):
                connect(svc)
        assert svc.ch_client is mock_client

    def test_connect_connection_error(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        with patch("hft_platform.recorder._loader_ch.get_ch_client", side_effect=ConnectionError("refused")):
            connect(svc)
        assert svc.ch_client is None

    def test_connect_timeout_error(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        with patch("hft_platform.recorder._loader_ch.get_ch_client", side_effect=TimeoutError("timeout")):
            connect(svc)
        assert svc.ch_client is None

    def test_connect_file_not_found_error(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        with patch("hft_platform.recorder._loader_ch.get_ch_client", side_effect=FileNotFoundError("no schema")):
            connect(svc)
        # FileNotFoundError doesn't set ch_client to None (only logs)

    def test_connect_generic_exception(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        with patch("hft_platform.recorder._loader_ch.get_ch_client", side_effect=RuntimeError("unexpected")):
            connect(svc)
        assert svc.ch_client is None

    def test_connect_schema_failure_does_not_crash(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        mock_client = MagicMock()
        with (
            patch("hft_platform.recorder._loader_ch.get_ch_client", return_value=mock_client),
            patch("hft_platform.recorder._loader_ch.apply_schema", side_effect=RuntimeError("schema fail")),
        ):
            connect(svc)
        assert svc.ch_client is mock_client

    def test_connect_view_repair_failure_does_not_crash(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        mock_client = MagicMock()
        with (
            patch("hft_platform.recorder._loader_ch.get_ch_client", return_value=mock_client),
            patch("hft_platform.recorder._loader_ch.apply_schema"),
            patch("hft_platform.recorder._loader_ch.ensure_price_scaled_views", side_effect=RuntimeError("view fail")),
        ):
            connect(svc)
        assert svc.ch_client is mock_client


class TestComputeBackoff:
    def test_connect_backoff_bounded(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        for attempt in range(10):
            delay = compute_connect_backoff(svc, attempt)
            assert delay >= 1.0
            assert delay <= svc._connect_max_backoff_s * 1.5 + 1.0

    def test_insert_backoff_bounded(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        for attempt in range(10):
            delay = compute_insert_backoff(svc, attempt)
            assert delay >= 0.1
            assert delay <= svc._insert_max_backoff_s * 1.5 + 0.1


class TestInsertWithRetry:
    def test_empty_data_returns_true(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        result = insert_with_retry(svc, "hft.orders", ["col"], [], "orders", 0)
        assert result is True

    def test_success_on_first_attempt(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.insert = MagicMock()
        result = insert_with_retry(svc, "hft.orders", ["col"], [[1]], "orders", 1)
        assert result is True

    def test_retry_then_success(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._insert_max_retries = 3
        call_count = [0]

        def flaky_insert(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient")

        svc.ch_client.insert = MagicMock(side_effect=flaky_insert)
        result = insert_with_retry(svc, "hft.orders", ["col"], [[1]], "orders", 1)
        assert result is True
        assert call_count[0] == 2

    def test_all_retries_fail(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._insert_max_retries = 2
        svc.ch_client.insert = MagicMock(side_effect=RuntimeError("persistent"))
        svc.metrics = MagicMock()
        result = insert_with_retry(svc, "hft.orders", ["col"], [[1]], "orders", 1)
        assert result is False

    def test_no_client_returns_false(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client = None
        svc.metrics = MagicMock()
        result = insert_with_retry(svc, "hft.orders", ["col"], [[1]], "orders", 1)
        assert result is False

    def test_success_with_metrics(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.insert = MagicMock()
        svc.metrics = MagicMock()
        result = insert_with_retry(svc, "hft.orders", ["col"], [[1]], "orders", 1)
        assert result is True
        svc.metrics.wal_replay_throughput_rows_total.inc.assert_called_with(1)


class TestDedupHelpers:
    def test_is_duplicate_returns_true(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.command.return_value = 1
        result = is_duplicate(svc, "orders", "abc123")
        assert result is True

    def test_is_duplicate_returns_false_on_zero(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.command.return_value = 0
        result = is_duplicate(svc, "orders", "abc123")
        assert result is False

    def test_is_duplicate_returns_false_on_exception(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.command.side_effect = RuntimeError("query failed")
        result = is_duplicate(svc, "orders", "abc123")
        assert result is False

    def test_record_dedup_success(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.insert = MagicMock()
        record_dedup(svc, "orders", "hash123", 5)
        svc.ch_client.insert.assert_called_once()

    def test_record_dedup_handles_exception(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.ch_client.insert = MagicMock(side_effect=RuntimeError("fail"))
        record_dedup(svc, "orders", "hash123", 5)
        # Should not raise

    def test_insert_with_dedup_empty_rows(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        result = insert_with_dedup(svc, "orders", [], "file.jsonl")
        assert result is True

    def test_insert_with_dedup_skips_duplicate(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._dedup_enabled = True
        svc._is_duplicate = MagicMock(return_value=True)
        result = insert_with_dedup(svc, "orders", [{"id": 1}], "file.jsonl")
        assert result is True
        svc.insert_batch.assert_not_called()

    def test_insert_with_dedup_inserts_and_records(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._dedup_enabled = True
        svc._is_duplicate = MagicMock(return_value=False)
        svc.insert_batch = MagicMock(return_value=True)
        svc._record_dedup = MagicMock()
        result = insert_with_dedup(svc, "orders", [{"id": 1}], "file.jsonl")
        assert result is True
        svc.insert_batch.assert_called_once()
        svc._record_dedup.assert_called_once()

    def test_insert_with_dedup_disabled_uses_insert_batch(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._dedup_enabled = False
        svc.insert_batch = MagicMock(return_value=True)
        result = insert_with_dedup(svc, "orders", [{"id": 1}], "file.jsonl")
        assert result is True
        svc.insert_batch.assert_called_once()


# ===========================================================================
# _loader_wal.py: process_single_file edge cases
# ===========================================================================


class TestProcessSingleFileEdgeCases:
    def test_connection_error_returns_false(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        fpath = _make_wal_file(svc.wal_dir, "market_data_100.jsonl", '{"x":1}\n')
        svc._insert_with_dedup = MagicMock(side_effect=ConnectionError("lost"))
        result = process_single_file(svc, fpath, force=True)
        assert result is False

    def test_partial_corruption_processes_valid_rows(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        os.makedirs(svc.archive_dir, exist_ok=True)
        content = "{bad json}\n" + json.dumps({"symbol": "2330"}) + "\n"
        fpath = _make_wal_file(svc.wal_dir, "market_data_200.jsonl", content)
        result = process_single_file(svc, fpath, force=True)
        assert result is True

    def test_batch_insert_failure_writes_to_dlq(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        os.makedirs(svc.archive_dir, exist_ok=True)
        lines = [
            json.dumps({"__wal_table__": "market_data", "__row_count__": 1}),
            json.dumps({"symbol": "2330"}),
        ]
        fpath = _make_wal_file(svc.wal_dir, "market_data_300.jsonl", "\n".join(lines) + "\n")
        svc._insert_with_dedup = MagicMock(return_value=False)
        result = process_single_file(svc, fpath, force=True)
        assert result is False
        svc._write_to_dlq.assert_called_once()

    def test_file_locked_by_writer_skipped(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        fpath = _make_wal_file(svc.wal_dir, "market_data_400.jsonl", '{"x":1}\n')
        with patch("fcntl.flock", side_effect=BlockingIOError("locked")):
            result = process_single_file(svc, fpath, force=True)
        assert result is False

    def test_mtime_check_oserror_returns_false(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        fpath = os.path.join(svc.wal_dir, "market_data_500.jsonl")
        # File does not exist, getmtime will raise OSError
        result = process_single_file(svc, fpath, force=False)
        assert result is False


class TestProcessFilesParallel:
    def test_parallel_processing(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._loader_concurrency = 2
        os.makedirs(svc.archive_dir, exist_ok=True)

        for i in range(3):
            _make_wal_file(svc.wal_dir, f"market_data_{i}.jsonl", json.dumps({"id": i}) + "\n")

        process_files(svc, force=True)
        assert svc._insert_with_dedup.call_count >= 1


class TestProcessFilesNoop:
    def test_no_files_returns_immediately(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        process_files(svc)
        svc._insert_with_dedup.assert_not_called()


# ===========================================================================
# _loader_dlq.py: replay and cleanup edge cases
# ===========================================================================


class TestReplayDlqEdgeCases:
    def test_exception_during_file_read_counts_as_failed(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        # Create a valid-looking DLQ file
        fpath = os.path.join(svc.dlq_dir, "market_data_100.jsonl")
        with open(fpath, "w") as f:
            f.write(json.dumps({"symbol": "X"}) + "\n")
        # Make insert_batch raise an unexpected exception
        svc.insert_batch = MagicMock(side_effect=RuntimeError("unexpected"))
        result = replay_dlq(svc)
        assert result["failed"] == 1


class TestCleanupEdgeCases:
    def test_dlq_cleanup_with_metrics_exception(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._dlq_retention_days = 0
        svc.metrics = MagicMock()
        svc.metrics.dlq_size_total.labels.side_effect = RuntimeError("metric fail")
        fpath = os.path.join(svc.dlq_dir, "old.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        os.utime(fpath, (0, 0))
        cleanup_old_dlq_files(svc)
        # File should still be deleted despite metrics failure
        assert not os.path.exists(fpath)

    def test_corrupt_cleanup_keeps_recent(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        os.makedirs(svc.corrupt_dir, exist_ok=True)
        fpath = os.path.join(svc.corrupt_dir, "recent.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        cleanup_old_corrupt_files(svc)
        assert os.path.exists(fpath)

    def test_archive_cleanup_keeps_recent(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        fpath = os.path.join(svc.archive_dir, "recent.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        cleanup_old_archive_files(svc)
        assert os.path.exists(fpath)


class TestCheckWalAccumulationEdgeCases:
    def test_critical_threshold_logged(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._wal_size_critical_mb = 0  # any size is "critical"
        svc._wal_size_warning_mb = 0
        svc.metrics = MagicMock()

        fpath = os.path.join(svc.wal_dir, "data.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n" * 100)
        check_wal_accumulation(svc)
        svc.metrics.wal_directory_size_bytes.set.assert_called()

    def test_drain_eta_with_processing(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc.metrics = MagicMock()
        svc._processed_files_total = 10
        svc._eta_sample_last_processed = 5
        svc._eta_sample_last_ts = time.time() - 10

        fpath = os.path.join(svc.wal_dir, "data.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        check_wal_accumulation(svc)
        svc.metrics.wal_drain_eta_seconds.set.assert_called()


# ===========================================================================
# schema.py tests
# ===========================================================================


class TestSchemaEdgeCases:
    def test_apply_schema_skips_already_applied(self, tmp_path) -> None:
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260101_001_test.sql").write_text("-- Up\nSELECT 1;\n")

        client = MagicMock()
        client.query.return_value.result_rows = [("20260101_001",)]

        with patch("hft_platform.recorder.schema.MIGRATIONS_DIR", str(migrations_dir)):
            apply_schema(client)

        # Only init calls, no migration insert
        client.insert.assert_not_called()

    def test_apply_schema_no_migration_files(self, tmp_path) -> None:
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        client = MagicMock()
        client.query.return_value.result_rows = []

        with patch("hft_platform.recorder.schema.MIGRATIONS_DIR", str(migrations_dir)):
            apply_schema(client)

        client.insert.assert_not_called()

    def test_apply_schema_statement_failure_raises(self, tmp_path) -> None:
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260101_001_fail.sql").write_text("-- Up\nBAD STATEMENT;\n")

        client = MagicMock()
        client.query.return_value.result_rows = []

        def command_side_effect(stmt):
            if "BAD STATEMENT" in stmt:
                raise RuntimeError("syntax error")

        client.command.side_effect = command_side_effect

        with patch("hft_platform.recorder.schema.MIGRATIONS_DIR", str(migrations_dir)):
            with pytest.raises(RuntimeError, match="syntax error"):
                apply_schema(client)

    def test_apply_schema_query_failure_raises_runtime(self, tmp_path) -> None:
        client = MagicMock()
        client.query.side_effect = RuntimeError("query failed")

        with pytest.raises(RuntimeError, match="Cannot determine applied migrations"):
            apply_schema(client)

    def test_view_uses_legacy_price_returns_false(self) -> None:
        client = MagicMock()
        assert _view_uses_legacy_price(client, "any_view") is False

    def test_execute_all_runs_statements(self) -> None:
        client = MagicMock()
        _execute_all(client, ["SELECT 1", "SELECT 2"])
        assert client.command.call_count == 2

    def test_ensure_price_scaled_views_noop(self) -> None:
        client = MagicMock()
        result = ensure_price_scaled_views(client)
        assert result is False
        client.command.assert_not_called()

    def test_apply_schema_single_part_filename(self, tmp_path) -> None:
        """Migration filename without enough underscores uses full name as version."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "simplemigration.sql").write_text("-- Up\nSELECT 1;\n")

        client = MagicMock()
        client.query.return_value.result_rows = []

        with patch("hft_platform.recorder.schema.MIGRATIONS_DIR", str(migrations_dir)):
            apply_schema(client)

        # Should have inserted the migration record
        client.insert.assert_called_once()


# ===========================================================================
# _loader_wal.py: emergency and pnl_snapshots table parsing
# ===========================================================================


class TestParseTableFromFilenameMore:
    def test_emergency_file_returns_unknown(self) -> None:
        assert parse_table_from_filename("emergency_dump_123.jsonl") == "unknown"

    def test_pnl_snapshots(self) -> None:
        assert parse_table_from_filename("pnl_snapshots_123.jsonl") == "pnl_snapshots"

    def test_hft_prefix_orders(self) -> None:
        assert parse_table_from_filename("hft.orders_123.jsonl") == "orders"

    def test_batch_filename(self) -> None:
        """batch_ prefixed files parse via batch format, not filename."""
        result = parse_table_from_filename("batch_12345_001.jsonl")
        assert isinstance(result, str)


class TestParseBatchTableNameMore:
    def test_pnl_snapshots(self) -> None:
        assert parse_batch_table_name("pnl_snapshots") == "pnl_snapshots"

    def test_latency_spans(self) -> None:
        assert parse_batch_table_name("latency_spans") == "latency_spans"

    def test_hft_prefix_fills(self) -> None:
        assert parse_batch_table_name("hft.fills") == "fills"


# ===========================================================================
# _loader_wal.py: save_manifest error handling
# ===========================================================================


class TestSaveManifestErrorHandling:
    def test_save_manifest_handles_write_error(self, tmp_path) -> None:
        svc = _make_svc(tmp_path)
        svc._manifest = {"file.jsonl"}
        # Make the manifest path a directory to cause write failure
        os.makedirs(svc._manifest_path, exist_ok=True)
        # Should not raise
        save_manifest(svc)

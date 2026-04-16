"""Coverage gap tests for recorder/_loader_dlq.py and recorder/_loader_wal.py.

Targets uncovered branches: DLQ write, DLQ replay with various file states,
DLQ/corrupt/archive cleanup, WAL accumulation checks, quarantine,
manifest load/save, file discovery, table name parsing, single-file processing
with various edge cases.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.recorder._loader_dlq import (
    check_wal_accumulation,
    cleanup_old_archive_files,
    cleanup_old_corrupt_files,
    cleanup_old_dlq_files,
    quarantine_corrupt_file,
    replay_dlq,
    write_to_dlq,
)
from hft_platform.recorder._loader_wal import (
    extract_file_ts,
    get_new_files,
    load_manifest,
    mark_processed,
    parse_batch_table_name,
    parse_table_from_filename,
    save_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    """Minimal service-like object with required attributes."""
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
        metrics=None,
        _manifest=set(),
        _manifest_path=str(tmp_path / "manifest.txt"),
        _manifest_enabled=True,
        _manifest_lock=threading.Lock(),
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
        _claim_registry=SimpleNamespace(
            try_claim=MagicMock(return_value=True),
            release_claim=MagicMock(),
        ),
        insert_batch=MagicMock(return_value=True),
        _insert_with_dedup=MagicMock(return_value=True),
        _write_to_dlq=MagicMock(),
        _quarantine_corrupt_file=MagicMock(),
    )


# ---------------------------------------------------------------------------
# extract_file_ts
# ---------------------------------------------------------------------------


class TestExtractFileTs:
    def test_standard_format(self):
        assert extract_file_ts("market_data_1234567890.jsonl") == 1234567890

    def test_batch_format(self):
        assert extract_file_ts("batch_9876543210_001.jsonl") == 9876543210

    def test_invalid_format(self):
        assert extract_file_ts("unknown.jsonl") == 0

    def test_no_underscore(self):
        assert extract_file_ts("nodata.jsonl") == 0


# ---------------------------------------------------------------------------
# parse_table_from_filename
# ---------------------------------------------------------------------------


class TestParseTableFromFilename:
    def test_market_data(self):
        assert parse_table_from_filename("market_data_123.jsonl") == "market_data"

    def test_orders(self):
        assert parse_table_from_filename("orders_123.jsonl") == "orders"

    def test_fills(self):
        assert parse_table_from_filename("fills_123.jsonl") == "fills"

    def test_risk_log(self):
        assert parse_table_from_filename("risk_log_123.jsonl") == "risk_log"

    def test_backtest_runs(self):
        assert parse_table_from_filename("backtest_runs_123.jsonl") == "backtest_runs"

    def test_latency_spans(self):
        assert parse_table_from_filename("latency_spans_123.jsonl") == "latency_spans"

    def test_pnl_snapshots(self):
        assert parse_table_from_filename("pnl_snapshots_123.jsonl") == "pnl_snapshots"

    def test_hft_prefix(self):
        assert parse_table_from_filename("hft.market_data_123.jsonl") == "market_data"

    def test_emergency_file(self):
        assert parse_table_from_filename("emergency_dump_123.jsonl") == "unknown"

    def test_unknown_file(self):
        result = parse_table_from_filename("custom_table_123.jsonl")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# parse_batch_table_name
# ---------------------------------------------------------------------------


class TestParseBatchTableName:
    def test_known_tables(self):
        assert parse_batch_table_name("market_data") == "market_data"
        assert parse_batch_table_name("orders") == "orders"
        assert parse_batch_table_name("fills") == "fills"
        assert parse_batch_table_name("risk_log") == "risk_log"
        assert parse_batch_table_name("logs") == "risk_log"
        assert parse_batch_table_name("trades") == "trades"

    def test_hft_prefix_stripped(self):
        assert parse_batch_table_name("hft.market_data") == "market_data"

    def test_unknown_table_raises(self):
        with pytest.raises(ValueError, match="Unknown table name"):
            parse_batch_table_name("nonexistent_table")


# ---------------------------------------------------------------------------
# load_manifest / save_manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_load_manifest_no_file(self, svc):
        load_manifest(svc)
        assert svc._manifest == set()

    def test_load_manifest_with_file(self, svc):
        with open(svc._manifest_path, "w") as f:
            f.write("file1.jsonl\nfile2.jsonl\n")
        load_manifest(svc)
        assert "file1.jsonl" in svc._manifest

    def test_load_manifest_detects_stuck_files(self, svc):
        """Stuck files (in manifest but still in WAL dir) are removed from manifest."""
        with open(svc._manifest_path, "w") as f:
            f.write("stuck.jsonl\n")
        # Create the stuck file in WAL dir
        with open(os.path.join(svc.wal_dir, "stuck.jsonl"), "w") as f:
            f.write("{}\n")
        load_manifest(svc)
        assert "stuck.jsonl" not in svc._manifest

    def test_save_manifest(self, svc):
        svc._manifest = {"a.jsonl", "b.jsonl"}
        save_manifest(svc)
        assert os.path.exists(svc._manifest_path)
        with open(svc._manifest_path) as f:
            content = f.read()
        assert "a.jsonl" in content
        assert "b.jsonl" in content

    def test_save_manifest_with_existing_backup(self, svc):
        svc._manifest = {"a.jsonl"}
        save_manifest(svc)
        svc._manifest = {"a.jsonl", "b.jsonl"}
        save_manifest(svc)  # Should create backup
        assert os.path.exists(svc._manifest_path + ".bak")


# ---------------------------------------------------------------------------
# get_new_files
# ---------------------------------------------------------------------------


class TestGetNewFiles:
    def test_no_manifest(self, svc):
        svc._manifest_enabled = False
        # Create a WAL file
        with open(os.path.join(svc.wal_dir, "market_data_100.jsonl"), "w") as f:
            f.write("{}\n")
        files = get_new_files(svc)
        assert len(files) == 1

    def test_with_manifest(self, svc):
        svc._manifest = {"old_file.jsonl"}
        with open(os.path.join(svc.wal_dir, "market_data_100.jsonl"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(svc.wal_dir, "old_file.jsonl"), "w") as f:
            f.write("{}\n")
        files = get_new_files(svc)
        assert len(files) == 1
        assert "market_data_100.jsonl" in files[0]

    def test_empty_dir(self, svc):
        files = get_new_files(svc)
        assert files == []


# ---------------------------------------------------------------------------
# mark_processed
# ---------------------------------------------------------------------------


class TestMarkProcessed:
    def test_mark_processed(self, svc):
        mark_processed(svc, "/path/to/market_data_100.jsonl")
        assert "market_data_100.jsonl" in svc._manifest

    def test_mark_processed_disabled(self, svc):
        svc._manifest_enabled = False
        mark_processed(svc, "/path/to/file.jsonl")
        assert len(svc._manifest) == 0


# ---------------------------------------------------------------------------
# write_to_dlq
# ---------------------------------------------------------------------------


class TestWriteToDlq:
    def test_write_to_dlq_basic(self, svc):
        rows = [{"a": 1}, {"b": 2}]
        write_to_dlq(svc, "test_table", rows, "test_error")
        dlq_files = os.listdir(svc.dlq_dir)
        assert len(dlq_files) == 1
        assert dlq_files[0].endswith(".jsonl")

    def test_write_to_dlq_with_metrics(self, svc):
        svc.metrics = MagicMock()
        svc.metrics.dlq_size_total = MagicMock()
        write_to_dlq(svc, "test", [{"a": 1}], "err")
        svc.metrics.dlq_size_total.labels.assert_called()


# ---------------------------------------------------------------------------
# replay_dlq
# ---------------------------------------------------------------------------


class TestReplayDlq:
    def test_replay_no_dir(self, svc):
        svc.dlq_dir = str(os.path.join(svc.dlq_dir, "nonexistent"))
        result = replay_dlq(svc)
        assert result["replayed"] == 0

    def test_replay_no_client(self, svc):
        svc.ch_client = None
        result = replay_dlq(svc)
        assert "no_client" in result["errors"]

    def test_replay_dry_run(self, svc):
        # Create a DLQ file
        import json
        fpath = os.path.join(svc.dlq_dir, "market_data_100.jsonl")
        with open(fpath, "w") as f:
            f.write(json.dumps({"_dlq_meta": True, "table": "market_data", "error": "e", "timestamp": 100, "row_count": 1}) + "\n")
            f.write(json.dumps({"val": 1}) + "\n")
        result = replay_dlq(svc, dry_run=True)
        assert result["replayed"] == 1

    def test_replay_success(self, svc):
        import json
        fpath = os.path.join(svc.dlq_dir, "market_data_100.jsonl")
        with open(fpath, "w") as f:
            f.write(json.dumps({"_dlq_meta": True, "table": "market_data", "error": "e", "timestamp": 100, "row_count": 1}) + "\n")
            f.write(json.dumps({"val": 1}) + "\n")
        result = replay_dlq(svc)
        assert result["replayed"] == 1

    def test_replay_insert_failure(self, svc):
        import json
        fpath = os.path.join(svc.dlq_dir, "market_data_100.jsonl")
        with open(fpath, "w") as f:
            f.write(json.dumps({"val": 1}) + "\n")
        svc.insert_batch = MagicMock(return_value=False)
        result = replay_dlq(svc)
        assert result["failed"] == 1

    def test_replay_empty_file(self, svc):
        fpath = os.path.join(svc.dlq_dir, "market_data_100.jsonl")
        with open(fpath, "w") as f:
            f.write("")
        result = replay_dlq(svc)
        assert result["skipped"] == 1

    def test_replay_unknown_table(self, svc):
        import json
        fpath = os.path.join(svc.dlq_dir, "emergency_100.jsonl")
        with open(fpath, "w") as f:
            f.write(json.dumps({"val": 1}) + "\n")
        result = replay_dlq(svc)
        assert result["skipped"] == 1

    def test_replay_max_files(self, svc):
        import json
        for i in range(5):
            fpath = os.path.join(svc.dlq_dir, f"market_data_{i}.jsonl")
            with open(fpath, "w") as f:
                f.write(json.dumps({"val": i}) + "\n")
        result = replay_dlq(svc, max_files=2)
        assert result["selected"] == 2


# ---------------------------------------------------------------------------
# cleanup_old_dlq_files
# ---------------------------------------------------------------------------


class TestCleanupOldDlqFiles:
    def test_cleanup_deletes_old(self, svc):
        svc._dlq_retention_days = 0  # All files are old
        fpath = os.path.join(svc.dlq_dir, "old_file.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        # Set old mtime
        os.utime(fpath, (0, 0))
        cleanup_old_dlq_files(svc)
        assert not os.path.exists(fpath)

    def test_cleanup_archives_old(self, svc):
        svc._dlq_retention_days = 0
        svc._dlq_archive_path = str(os.path.join(svc.dlq_dir, "archived"))
        fpath = os.path.join(svc.dlq_dir, "old_file.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        os.utime(fpath, (0, 0))
        cleanup_old_dlq_files(svc)
        assert os.path.exists(os.path.join(svc._dlq_archive_path, "old_file.jsonl"))

    def test_cleanup_no_dir(self, svc):
        svc.dlq_dir = "/nonexistent_dir"
        cleanup_old_dlq_files(svc)  # Should not raise

    def test_cleanup_rate_limited(self, svc):
        svc._last_dlq_cleanup_ts = time.time() + 9999
        svc._dlq_cleanup_interval_s = 99999
        cleanup_old_dlq_files(svc)  # Should be rate-limited


# ---------------------------------------------------------------------------
# cleanup_old_corrupt_files
# ---------------------------------------------------------------------------


class TestCleanupCorruptFiles:
    def test_cleanup_deletes_old(self, svc):
        svc._corrupt_retention_days = 0
        os.makedirs(svc.corrupt_dir, exist_ok=True)
        fpath = os.path.join(svc.corrupt_dir, "bad.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        os.utime(fpath, (0, 0))
        cleanup_old_corrupt_files(svc)
        assert not os.path.exists(fpath)

    def test_cleanup_no_dir(self, svc):
        svc.corrupt_dir = "/nonexistent_corrupt"
        cleanup_old_corrupt_files(svc)  # Should not raise


# ---------------------------------------------------------------------------
# cleanup_old_archive_files
# ---------------------------------------------------------------------------


class TestCleanupArchiveFiles:
    def test_cleanup_deletes_old(self, svc):
        svc._archive_retention_days = 0
        fpath = os.path.join(svc.archive_dir, "old.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        os.utime(fpath, (0, 0))
        cleanup_old_archive_files(svc)
        assert not os.path.exists(fpath)

    def test_cleanup_no_dir(self, svc):
        svc.archive_dir = "/nonexistent_archive"
        cleanup_old_archive_files(svc)  # Should not raise

    def test_cleanup_skips_non_jsonl(self, svc):
        svc._archive_retention_days = 0
        fpath = os.path.join(svc.archive_dir, "readme.txt")
        with open(fpath, "w") as f:
            f.write("not a WAL file")
        os.utime(fpath, (0, 0))
        cleanup_old_archive_files(svc)
        assert os.path.exists(fpath)  # Should not be deleted


# ---------------------------------------------------------------------------
# check_wal_accumulation
# ---------------------------------------------------------------------------


class TestCheckWalAccumulation:
    def test_basic_check(self, svc):
        svc.metrics = MagicMock()
        fpath = os.path.join(svc.wal_dir, "data_100.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n" * 100)
        check_wal_accumulation(svc)
        svc.metrics.wal_directory_size_bytes.set.assert_called()

    def test_no_dir(self, svc):
        svc.wal_dir = "/nonexistent_wal"
        check_wal_accumulation(svc)

    def test_rate_limited(self, svc):
        svc._last_wal_check_ts = time.time() + 9999
        svc._wal_check_interval_s = 99999
        check_wal_accumulation(svc)

    def test_warning_threshold(self, svc):
        svc.metrics = MagicMock()
        svc._wal_size_warning_mb = 0  # Trigger warning
        fpath = os.path.join(svc.wal_dir, "data_100.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n" * 10)
        check_wal_accumulation(svc)

    def test_no_wal_files(self, svc):
        svc.metrics = MagicMock()
        check_wal_accumulation(svc)
        svc.metrics.wal_file_count.set.assert_called_with(0)


# ---------------------------------------------------------------------------
# quarantine_corrupt_file
# ---------------------------------------------------------------------------


class TestQuarantineCorruptFile:
    def test_quarantine(self, svc):
        fpath = os.path.join(svc.wal_dir, "bad.jsonl")
        with open(fpath, "w") as f:
            f.write("corrupt\n")
        quarantine_corrupt_file(svc, fpath, "bad.jsonl", "test_reason")
        assert os.path.exists(os.path.join(svc.corrupt_dir, "bad.jsonl"))

    def test_quarantine_failure(self, svc):
        """Quarantine handles missing source gracefully."""
        quarantine_corrupt_file(svc, "/nonexistent/bad.jsonl", "bad.jsonl", "missing")
        # Should not raise

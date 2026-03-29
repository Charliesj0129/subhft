"""Unit tests for hft_platform.recorder._loader_dlq.

Covers: write_to_dlq, replay_dlq, cleanup_old_dlq_files,
cleanup_old_corrupt_files, cleanup_old_archive_files,
check_wal_accumulation, quarantine_corrupt_file.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_svc(tmp_path) -> Any:
    """Build a minimal mock WALLoaderService-like object."""
    svc = MagicMock()
    svc.dlq_dir = str(tmp_path / "dlq")
    svc.archive_dir = str(tmp_path / "archive")
    svc.corrupt_dir = str(tmp_path / "corrupt")
    svc.wal_dir = str(tmp_path / "wal")
    svc.metrics = None
    svc.ch_client = MagicMock()
    svc._dlq_retention_days = 7
    svc._corrupt_retention_days = 7
    svc._archive_retention_days = 30
    svc._dlq_cleanup_interval_s = 0  # always run in tests
    svc._dlq_archive_path = None
    svc._last_dlq_cleanup_ts = 0.0
    svc._last_corrupt_cleanup_ts = 0.0
    svc._last_archive_cleanup_ts = 0.0
    svc._last_wal_check_ts = 0.0
    svc._wal_check_interval_s = 0  # always run in tests
    svc._wal_size_critical_mb = 500
    svc._wal_size_warning_mb = 100
    svc._processed_files_total = 0
    svc._eta_sample_last_ts = 0.0
    svc._eta_sample_last_processed = 0
    return svc


def _make_old_file(directory: str, filename: str, content: str, age_s: float = 86400 * 10) -> str:
    """Write a file and set its mtime to `age_s` seconds in the past."""
    os.makedirs(directory, exist_ok=True)
    fpath = os.path.join(directory, filename)
    with open(fpath, "w") as f:
        f.write(content)
    past = time.time() - age_s
    os.utime(fpath, (past, past))
    return fpath


# ---------------------------------------------------------------------------
# write_to_dlq
# ---------------------------------------------------------------------------


class TestWriteToDlq:
    def test_creates_dlq_directory_and_file(self, tmp_path):
        svc = _make_svc(tmp_path)
        rows = [{"symbol": "2330", "price": 1000}]
        write_to_dlq(svc, "market_data", rows, "test_error")

        assert os.path.isdir(svc.dlq_dir)
        files = os.listdir(svc.dlq_dir)
        assert len(files) == 1
        assert files[0].startswith("market_data_")
        assert files[0].endswith(".jsonl")

    def test_dlq_file_contains_meta_and_rows(self, tmp_path):
        svc = _make_svc(tmp_path)
        rows = [{"a": 1}, {"b": 2}]
        write_to_dlq(svc, "orders", rows, "insert_failed")

        dlq_files = os.listdir(svc.dlq_dir)
        fpath = os.path.join(svc.dlq_dir, dlq_files[0])
        lines = [l for l in open(fpath).read().splitlines() if l]

        meta = json.loads(lines[0])
        assert meta["_dlq_meta"] is True
        assert meta["table"] == "orders"
        assert meta["error"] == "insert_failed"
        assert meta["row_count"] == 2

        assert json.loads(lines[1]) == {"a": 1}
        assert json.loads(lines[2]) == {"b": 2}

    def test_increments_metrics_when_present(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc.metrics = MagicMock()
        write_to_dlq(svc, "market_data", [{}], "err")
        svc.metrics.dlq_size_total.labels.assert_called_once_with(source="recorder")

    def test_metrics_exception_does_not_raise(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc.metrics = MagicMock()
        svc.metrics.dlq_size_total.labels.side_effect = RuntimeError("bad")
        # Should not propagate
        write_to_dlq(svc, "market_data", [{}], "err")

    def test_write_error_is_logged_not_raised(self, tmp_path):
        svc = _make_svc(tmp_path)
        # Simulate open() failure after makedirs succeeds
        os.makedirs(svc.dlq_dir, exist_ok=True)
        with patch("builtins.open", side_effect=OSError("disk full")):
            # write_to_dlq catches exceptions internally
            write_to_dlq(svc, "market_data", [{}], "err")  # should not raise


# ---------------------------------------------------------------------------
# replay_dlq
# ---------------------------------------------------------------------------


class TestReplayDlq:
    def test_returns_zeros_when_dlq_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        result = replay_dlq(svc)
        assert result == {"replayed": 0, "skipped": 0, "failed": 0, "errors": []}

    def test_returns_error_when_no_client_and_not_dry_run(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        svc.ch_client = None
        result = replay_dlq(svc, dry_run=False)
        assert result["errors"] == ["no_client"]
        assert result["replayed"] == 0

    def test_dry_run_counts_replayed_without_insert(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)

        rows = [{"symbol": "X", "price": 100}]
        # Write a proper DLQ file manually
        meta = json.dumps({"_dlq_meta": True, "table": "market_data", "error": "e", "timestamp": 1, "row_count": 1})
        content = meta + "\n" + json.dumps(rows[0]) + "\n"
        fname = "market_data_1000.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write(content)

        result = replay_dlq(svc, dry_run=True)
        assert result["replayed"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0
        # File still in dlq (dry run - not moved)
        assert os.path.exists(os.path.join(svc.dlq_dir, fname))

    def test_successful_replay_moves_to_archive(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)

        rows = [{"symbol": "2330", "price": 100}]
        meta = json.dumps({"_dlq_meta": True, "table": "market_data", "error": "e", "timestamp": 2, "row_count": 1})
        fname = "market_data_2000.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write(meta + "\n" + json.dumps(rows[0]) + "\n")

        svc.insert_batch = MagicMock(return_value=True)
        result = replay_dlq(svc)
        assert result["replayed"] == 1
        assert not os.path.exists(os.path.join(svc.dlq_dir, fname))
        assert os.path.exists(os.path.join(svc.archive_dir, fname))

    def test_failed_insert_increments_failed_count(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)

        rows = [{"symbol": "2330", "price": 100}]
        meta = json.dumps({"_dlq_meta": True, "table": "orders", "error": "e", "timestamp": 3, "row_count": 1})
        fname = "orders_3000.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write(meta + "\n" + json.dumps(rows[0]) + "\n")

        svc.insert_batch = MagicMock(return_value=False)
        result = replay_dlq(svc)
        assert result["failed"] == 1
        assert fname in result["errors"]
        # File stays in DLQ
        assert os.path.exists(os.path.join(svc.dlq_dir, fname))

    def test_empty_file_is_skipped_and_archived(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)

        # File with only meta line, no data rows
        meta = json.dumps({"_dlq_meta": True, "table": "market_data", "error": "e", "timestamp": 4, "row_count": 0})
        fname = "market_data_4000.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write(meta + "\n")

        svc.insert_batch = MagicMock(return_value=True)
        result = replay_dlq(svc)
        assert result["skipped"] == 1
        assert result["replayed"] == 0

    def test_unknown_table_filename_is_skipped(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)

        # A filename that produces an empty base → parse_table_from_filename returns "unknown"
        # e.g. "_9999.jsonl" → split by "_" removes last part → base = "" → "unknown"
        fname = "_9999.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write(json.dumps({"data": 1}) + "\n")

        result = replay_dlq(svc)
        assert result["skipped"] == 1

    def test_max_files_limits_processing(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)
        svc.ch_client = None  # force no-client path for simple test

        # Create 3 files
        for i in range(3):
            with open(os.path.join(svc.dlq_dir, f"market_data_{i}.jsonl"), "w") as f:
                f.write("{}\n")

        result = replay_dlq(svc, dry_run=True, max_files=2)
        assert result["max_files"] == 2
        assert result["selected"] == 2

    def test_corrupt_json_line_is_skipped_gracefully(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        os.makedirs(svc.archive_dir)

        fname = "market_data_5000.jsonl"
        with open(os.path.join(svc.dlq_dir, fname), "w") as f:
            f.write("{bad json}\n")
            f.write(json.dumps({"symbol": "X"}) + "\n")

        svc.insert_batch = MagicMock(return_value=True)
        # Should not raise; corrupt lines are skipped
        result = replay_dlq(svc)
        assert result["replayed"] + result["skipped"] + result["failed"] >= 0


# ---------------------------------------------------------------------------
# cleanup_old_dlq_files
# ---------------------------------------------------------------------------


class TestCleanupOldDlqFiles:
    def test_skips_if_interval_not_elapsed(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._dlq_cleanup_interval_s = 9999
        svc._last_dlq_cleanup_ts = time.time()  # just ran
        os.makedirs(svc.dlq_dir)
        _make_old_file(svc.dlq_dir, "market_data_1.jsonl", "{}")

        cleanup_old_dlq_files(svc)
        assert os.path.exists(os.path.join(svc.dlq_dir, "market_data_1.jsonl"))

    def test_deletes_old_files_when_no_archive_path(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        fpath = _make_old_file(svc.dlq_dir, "market_data_old.jsonl", "{}")

        cleanup_old_dlq_files(svc)
        assert not os.path.exists(fpath)

    def test_archives_old_files_when_archive_path_set(self, tmp_path):
        svc = _make_svc(tmp_path)
        archive_path = str(tmp_path / "dlq_archive")
        svc._dlq_archive_path = archive_path
        os.makedirs(svc.dlq_dir)
        fname = "market_data_old2.jsonl"
        _make_old_file(svc.dlq_dir, fname, "{}")

        cleanup_old_dlq_files(svc)
        assert not os.path.exists(os.path.join(svc.dlq_dir, fname))
        assert os.path.exists(os.path.join(archive_path, fname))

    def test_skips_recent_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.dlq_dir)
        # File modified NOW — should not be deleted
        fname = "market_data_recent.jsonl"
        fpath = os.path.join(svc.dlq_dir, fname)
        with open(fpath, "w") as f:
            f.write("{}")

        cleanup_old_dlq_files(svc)
        assert os.path.exists(fpath)

    def test_noop_when_dlq_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        # dlq_dir does not exist
        cleanup_old_dlq_files(svc)  # should not raise

    def test_increments_metrics_after_cleanup(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc.metrics = MagicMock()
        os.makedirs(svc.dlq_dir)
        _make_old_file(svc.dlq_dir, "market_data_x.jsonl", "{}")

        cleanup_old_dlq_files(svc)
        svc.metrics.dlq_size_total.labels.assert_called_once_with(source="cleanup")


# ---------------------------------------------------------------------------
# cleanup_old_corrupt_files
# ---------------------------------------------------------------------------


class TestCleanupOldCorruptFiles:
    def test_skips_if_interval_not_elapsed(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._dlq_cleanup_interval_s = 9999
        svc._last_corrupt_cleanup_ts = time.time()
        os.makedirs(svc.corrupt_dir)
        fpath = _make_old_file(svc.corrupt_dir, "corrupt_1.jsonl", "{}")

        cleanup_old_corrupt_files(svc)
        assert os.path.exists(fpath)

    def test_deletes_old_corrupt_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.corrupt_dir)
        fpath = _make_old_file(svc.corrupt_dir, "corrupt_old.jsonl", "{}")

        cleanup_old_corrupt_files(svc)
        assert not os.path.exists(fpath)

    def test_keeps_recent_corrupt_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.corrupt_dir)
        fpath = os.path.join(svc.corrupt_dir, "corrupt_recent.jsonl")
        with open(fpath, "w") as f:
            f.write("{}")

        cleanup_old_corrupt_files(svc)
        assert os.path.exists(fpath)

    def test_noop_when_corrupt_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        cleanup_old_corrupt_files(svc)  # should not raise


# ---------------------------------------------------------------------------
# cleanup_old_archive_files
# ---------------------------------------------------------------------------


class TestCleanupOldArchiveFiles:
    def test_deletes_old_archive_jsonl_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._archive_retention_days = 7  # 7 days retention; our file is 10 days old
        os.makedirs(svc.archive_dir)
        fpath = _make_old_file(svc.archive_dir, "market_data_old.jsonl", "{}")

        cleanup_old_archive_files(svc)
        assert not os.path.exists(fpath)

    def test_skips_non_jsonl_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.archive_dir)
        fpath = _make_old_file(svc.archive_dir, "readme.txt", "hello")

        cleanup_old_archive_files(svc)
        # Non-jsonl files should not be touched
        assert os.path.exists(fpath)

    def test_noop_when_archive_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        cleanup_old_archive_files(svc)  # should not raise

    def test_skips_if_interval_not_elapsed(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._dlq_cleanup_interval_s = 9999
        svc._last_archive_cleanup_ts = time.time()
        os.makedirs(svc.archive_dir)
        fpath = _make_old_file(svc.archive_dir, "market_data_skip.jsonl", "{}")

        cleanup_old_archive_files(svc)
        assert os.path.exists(fpath)


# ---------------------------------------------------------------------------
# check_wal_accumulation
# ---------------------------------------------------------------------------


class TestCheckWalAccumulation:
    def test_noop_when_wal_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        check_wal_accumulation(svc)  # should not raise

    def test_skips_if_interval_not_elapsed(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._wal_check_interval_s = 9999
        svc._last_wal_check_ts = time.time()
        os.makedirs(svc.wal_dir)
        check_wal_accumulation(svc)  # should return early

    def test_sets_metrics_when_present(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)

        # Create two WAL files
        for name in ("market_data_1.jsonl", "market_data_2.jsonl"):
            with open(os.path.join(svc.wal_dir, name), "w") as f:
                f.write('{"x": 1}\n')

        svc.metrics = MagicMock()
        check_wal_accumulation(svc)

        svc.metrics.wal_directory_size_bytes.set.assert_called()
        svc.metrics.wal_file_count.set.assert_called()
        svc.metrics.wal_oldest_file_age_seconds.set.assert_called()
        svc.metrics.wal_backlog_files.set.assert_called()

    def test_no_metrics_no_error(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc.metrics = None
        os.makedirs(svc.wal_dir)
        with open(os.path.join(svc.wal_dir, "market_data_1.jsonl"), "w") as f:
            f.write("{}\n")
        check_wal_accumulation(svc)  # should not raise

    def test_ignores_non_jsonl_files_in_wal_dir(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)

        # A non-jsonl file should be ignored
        with open(os.path.join(svc.wal_dir, "readme.txt"), "w") as f:
            f.write("hello")

        svc.metrics = MagicMock()
        check_wal_accumulation(svc)

        # file_count should be 0 (only .jsonl counted)
        svc.metrics.wal_file_count.set.assert_called_with(0)

    def test_empty_wal_dir_sets_zero_metrics(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        svc.metrics = MagicMock()
        check_wal_accumulation(svc)
        svc.metrics.wal_file_count.set.assert_called_with(0)


# ---------------------------------------------------------------------------
# quarantine_corrupt_file
# ---------------------------------------------------------------------------


class TestQuarantineCorruptFile:
    def test_moves_file_to_corrupt_dir(self, tmp_path):
        svc = _make_svc(tmp_path)
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()

        fname = "market_data_bad.jsonl"
        fpath = str(wal_dir / fname)
        with open(fpath, "w") as f:
            f.write("badjson\n")

        quarantine_corrupt_file(svc, fpath, fname, "all_lines_corrupt")

        assert not os.path.exists(fpath)
        assert os.path.exists(os.path.join(svc.corrupt_dir, fname))

    def test_creates_corrupt_dir_if_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()

        fname = "market_data_bad2.jsonl"
        fpath = str(wal_dir / fname)
        with open(fpath, "w") as f:
            f.write("badjson\n")

        assert not os.path.isdir(svc.corrupt_dir)
        quarantine_corrupt_file(svc, fpath, fname, "reason")
        assert os.path.isdir(svc.corrupt_dir)

    def test_handles_move_error_gracefully(self, tmp_path):
        svc = _make_svc(tmp_path)
        # fpath does not exist → shutil.move will fail
        quarantine_corrupt_file(svc, "/nonexistent/path/bad.jsonl", "bad.jsonl", "reason")
        # Should not raise

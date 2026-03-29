"""Unit tests for hft_platform.recorder._loader_wal.

Covers: load_manifest, save_manifest, extract_file_ts, get_new_files,
mark_processed, parse_table_from_filename, parse_batch_table_name,
process_single_file, process_files.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder._loader_wal import (
    extract_file_ts,
    get_new_files,
    load_manifest,
    mark_processed,
    parse_batch_table_name,
    parse_table_from_filename,
    process_files,
    process_single_file,
    save_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_svc(tmp_path) -> MagicMock:
    """Build a minimal mock service compatible with _loader_wal functions."""
    svc = MagicMock()
    svc.wal_dir = str(tmp_path / "wal")
    svc.archive_dir = str(tmp_path / "archive")
    svc.corrupt_dir = str(tmp_path / "corrupt")
    svc._manifest_path = str(tmp_path / "manifest.txt")
    svc._manifest: set = set()
    svc._manifest_enabled = True
    svc._manifest_lock = threading.Lock()
    svc._strict_order = False
    svc._last_processed_ts = 0
    svc._processed_files_total = 0
    svc._loader_concurrency = 1
    svc.ch_client = MagicMock()
    svc._claim_registry = MagicMock()
    svc._claim_registry.try_claim = MagicMock(return_value=True)
    svc._claim_registry.release_claim = MagicMock()
    svc._insert_with_dedup = MagicMock(return_value=True)
    svc._write_to_dlq = MagicMock()
    svc._quarantine_corrupt_file = MagicMock()
    return svc


def _make_wal_file(wal_dir: str, filename: str, content: str, age_s: float = 10) -> str:
    os.makedirs(wal_dir, exist_ok=True)
    fpath = os.path.join(wal_dir, filename)
    with open(fpath, "w") as f:
        f.write(content)
    past = time.time() - age_s
    os.utime(fpath, (past, past))
    return fpath


# ---------------------------------------------------------------------------
# extract_file_ts
# ---------------------------------------------------------------------------


class TestExtractFileTs:
    def test_valid_filename(self):
        assert extract_file_ts("market_data_1234567890.jsonl") == 1234567890

    def test_underscore_in_table_name(self):
        assert extract_file_ts("risk_log_9999.jsonl") == 9999

    def test_returns_zero_on_non_numeric_suffix(self):
        assert extract_file_ts("market_data_abc.jsonl") == 0

    def test_returns_zero_on_no_underscore(self):
        assert extract_file_ts("nodash.jsonl") == 0

    def test_empty_string(self):
        assert extract_file_ts("") == 0


# ---------------------------------------------------------------------------
# parse_table_from_filename
# ---------------------------------------------------------------------------


class TestParseTableFromFilename:
    def test_market_data(self):
        assert parse_table_from_filename("market_data_123.jsonl") == "market_data"

    def test_orders(self):
        assert parse_table_from_filename("orders_456.jsonl") == "orders"

    def test_fills_maps_to_trades(self):
        assert parse_table_from_filename("fills_789.jsonl") == "trades"

    def test_risk_log(self):
        assert parse_table_from_filename("risk_log_111.jsonl") == "risk_log"

    def test_backtest_runs(self):
        assert parse_table_from_filename("backtest_runs_222.jsonl") == "backtest_runs"

    def test_latency_spans(self):
        assert parse_table_from_filename("latency_spans_333.jsonl") == "latency_spans"

    def test_hft_prefix_stripped(self):
        assert parse_table_from_filename("hft.market_data_100.jsonl") == "market_data"

    def test_unknown_table(self):
        result = parse_table_from_filename("completely_unknown_table_999.jsonl")
        # Should return something (the base prefix), not crash
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# parse_batch_table_name
# ---------------------------------------------------------------------------


class TestParseBatchTableName:
    def test_market_data(self):
        assert parse_batch_table_name("market_data") == "market_data"

    def test_orders(self):
        assert parse_batch_table_name("orders") == "orders"

    def test_fills_to_trades(self):
        assert parse_batch_table_name("fills") == "trades"

    def test_trades_to_trades(self):
        assert parse_batch_table_name("trades") == "trades"

    def test_logs_to_risk_log(self):
        assert parse_batch_table_name("logs") == "risk_log"

    def test_risk_log(self):
        assert parse_batch_table_name("risk_log") == "risk_log"

    def test_backtest_runs(self):
        assert parse_batch_table_name("backtest_runs") == "backtest_runs"

    def test_latency_spans(self):
        assert parse_batch_table_name("latency_spans") == "latency_spans"

    def test_hft_prefix_stripped(self):
        assert parse_batch_table_name("hft.market_data") == "market_data"

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown table name"):
            parse_batch_table_name("nonexistent_table")


# ---------------------------------------------------------------------------
# load_manifest / save_manifest
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def test_empty_manifest_when_file_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        load_manifest(svc)
        assert svc._manifest == set()

    def test_loads_existing_manifest(self, tmp_path):
        svc = _make_svc(tmp_path)
        with open(svc._manifest_path, "w") as f:
            f.write("market_data_1.jsonl\norders_2.jsonl\n")
        load_manifest(svc)
        assert svc._manifest == {"market_data_1.jsonl", "orders_2.jsonl"}

    def test_removes_stuck_files_from_manifest(self, tmp_path):
        """EC-5: files still in WAL dir but in manifest should be removed from manifest."""
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        # Create a WAL file that is still present
        stuck_fname = "market_data_stuck.jsonl"
        with open(os.path.join(svc.wal_dir, stuck_fname), "w") as f:
            f.write("{}\n")
        with open(svc._manifest_path, "w") as f:
            f.write(stuck_fname + "\n")
        load_manifest(svc)
        assert stuck_fname not in svc._manifest

    def test_corrupt_manifest_resets_to_empty(self, tmp_path):
        svc = _make_svc(tmp_path)
        # Write a manifest that causes an OS error on open (simulate by making dir not file)
        os.makedirs(svc._manifest_path)  # path exists as directory
        load_manifest(svc)
        assert svc._manifest == set()


class TestSaveManifest:
    def test_saves_and_reloads(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._manifest = {"file_a.jsonl", "file_b.jsonl"}
        save_manifest(svc)
        assert os.path.exists(svc._manifest_path)
        loaded = {line.strip() for line in open(svc._manifest_path) if line.strip()}
        assert loaded == {"file_a.jsonl", "file_b.jsonl"}

    def test_creates_backup_of_existing_manifest(self, tmp_path):
        svc = _make_svc(tmp_path)
        with open(svc._manifest_path, "w") as f:
            f.write("old.jsonl\n")
        svc._manifest = {"new.jsonl"}
        save_manifest(svc)
        bak = svc._manifest_path + ".bak"
        assert os.path.exists(bak)
        assert "old.jsonl" in open(bak).read()

    def test_empty_manifest_saves_empty_file(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._manifest = set()
        save_manifest(svc)
        content = open(svc._manifest_path).read().strip()
        assert content == ""


# ---------------------------------------------------------------------------
# get_new_files
# ---------------------------------------------------------------------------


class TestGetNewFiles:
    def test_returns_files_not_in_manifest(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        with open(os.path.join(svc.wal_dir, "market_data_1.jsonl"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(svc.wal_dir, "market_data_2.jsonl"), "w") as f:
            f.write("{}\n")
        svc._manifest = {"market_data_1.jsonl"}

        files = get_new_files(svc)
        fnames = [os.path.basename(f) for f in files]
        assert "market_data_2.jsonl" in fnames
        assert "market_data_1.jsonl" not in fnames

    def test_returns_empty_when_wal_dir_missing(self, tmp_path):
        svc = _make_svc(tmp_path)
        files = get_new_files(svc)
        assert files == []

    def test_manifest_disabled_returns_all_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._manifest_enabled = False
        os.makedirs(svc.wal_dir)
        with open(os.path.join(svc.wal_dir, "market_data_10.jsonl"), "w") as f:
            f.write("{}\n")
        svc._manifest = {"market_data_10.jsonl"}  # should be ignored

        files = get_new_files(svc)
        fnames = [os.path.basename(f) for f in files]
        assert "market_data_10.jsonl" in fnames

    def test_files_sorted_by_timestamp(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        for ts in (300, 100, 200):
            with open(os.path.join(svc.wal_dir, f"market_data_{ts}.jsonl"), "w") as f:
                f.write("{}\n")
        svc._manifest = set()

        files = get_new_files(svc)
        ts_values = [extract_file_ts(os.path.basename(f)) for f in files]
        assert ts_values == sorted(ts_values)


# ---------------------------------------------------------------------------
# mark_processed
# ---------------------------------------------------------------------------


class TestMarkProcessed:
    def test_adds_basename_to_manifest(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._manifest = set()
        mark_processed(svc, "/some/path/market_data_1.jsonl")
        assert "market_data_1.jsonl" in svc._manifest

    def test_noop_when_manifest_disabled(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._manifest_enabled = False
        svc._manifest = set()
        mark_processed(svc, "/some/path/market_data_1.jsonl")
        assert len(svc._manifest) == 0


# ---------------------------------------------------------------------------
# process_single_file
# ---------------------------------------------------------------------------


class TestProcessSingleFile:
    def test_skips_when_claim_fails(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._claim_registry.try_claim.return_value = False
        os.makedirs(svc.wal_dir)
        fpath = _make_wal_file(svc.wal_dir, "market_data_1.jsonl", "{}\n")
        result = process_single_file(svc, fpath)
        assert result is False

    def test_skips_recent_file(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        # Write file with NOW as mtime (not old enough)
        fpath = os.path.join(svc.wal_dir, "market_data_999.jsonl")
        with open(fpath, "w") as f:
            f.write("{}\n")
        result = process_single_file(svc, fpath, force=False)
        assert result is False

    def test_processes_old_file_successfully(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)
        row = {"symbol": "2330", "price": 1000}
        fpath = _make_wal_file(svc.wal_dir, "market_data_100.jsonl", json.dumps(row) + "\n")

        result = process_single_file(svc, fpath, force=True)
        assert result is True
        assert not os.path.exists(fpath)
        assert os.path.exists(os.path.join(svc.archive_dir, "market_data_100.jsonl"))

    def test_skips_file_with_unknown_table(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        # Filename that maps to "unknown"
        fpath = _make_wal_file(svc.wal_dir, "zzz.jsonl", '{"x":1}\n')
        result = process_single_file(svc, fpath, force=True)
        assert result is False

    def test_all_corrupt_lines_quarantines_file(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        fpath = _make_wal_file(svc.wal_dir, "market_data_200.jsonl", "{bad json}\n")
        result = process_single_file(svc, fpath, force=True)
        assert result is False
        svc._quarantine_corrupt_file.assert_called_once()

    def test_insert_failure_writes_to_dlq(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._insert_with_dedup = MagicMock(return_value=False)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)
        row = {"symbol": "2330"}
        fpath = _make_wal_file(svc.wal_dir, "market_data_300.jsonl", json.dumps(row) + "\n")
        result = process_single_file(svc, fpath, force=True)
        assert result is False
        svc._write_to_dlq.assert_called_once()

    def test_empty_file_archived_without_insert(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)
        fpath = _make_wal_file(svc.wal_dir, "market_data_400.jsonl", "")
        result = process_single_file(svc, fpath, force=True)
        assert result is True
        svc._insert_with_dedup.assert_not_called()

    def test_strict_order_rejects_out_of_order_file(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc._strict_order = True
        svc._last_processed_ts = 9999999
        os.makedirs(svc.wal_dir)
        fpath = _make_wal_file(svc.wal_dir, "market_data_100.jsonl", '{"x":1}\n')
        result = process_single_file(svc, fpath, force=True)
        assert result is False

    def test_batch_format_multi_table(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)

        # Build a multi-table batch WAL file
        lines = [
            json.dumps({"__wal_table__": "market_data"}),
            json.dumps({"symbol": "2330", "price": 100}),
            json.dumps({"__wal_table__": "orders"}),
            json.dumps({"order_id": "abc"}),
        ]
        fpath = _make_wal_file(svc.wal_dir, "market_data_500.jsonl", "\n".join(lines) + "\n")
        result = process_single_file(svc, fpath, force=True)
        assert result is True
        assert svc._insert_with_dedup.call_count == 2

    def test_batch_format_unknown_table_returns_false(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)

        lines = [
            json.dumps({"__wal_table__": "unknown_nonexistent_table"}),
            json.dumps({"data": 1}),
        ]
        fpath = _make_wal_file(svc.wal_dir, "market_data_600.jsonl", "\n".join(lines) + "\n")
        result = process_single_file(svc, fpath, force=True)
        assert result is False

    def test_file_not_found_returns_false(self, tmp_path):
        svc = _make_svc(tmp_path)
        result = process_single_file(svc, "/nonexistent/path/market_data_1.jsonl", force=True)
        assert result is False


# ---------------------------------------------------------------------------
# process_files
# ---------------------------------------------------------------------------


class TestProcessFiles:
    def test_noop_when_no_ch_client(self, tmp_path):
        svc = _make_svc(tmp_path)
        svc.ch_client = None
        os.makedirs(svc.wal_dir)
        _make_wal_file(svc.wal_dir, "market_data_1.jsonl", '{"x":1}\n')
        # Should return without processing
        process_files(svc)
        svc._insert_with_dedup.assert_not_called()

    def test_processes_file_and_saves_manifest(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        os.makedirs(svc.archive_dir)
        row = {"symbol": "2330"}
        fpath = _make_wal_file(svc.wal_dir, "market_data_1000.jsonl", json.dumps(row) + "\n")

        process_files(svc, force=True)
        assert not os.path.exists(fpath)
        assert os.path.exists(svc._manifest_path)

    def test_noop_when_no_files(self, tmp_path):
        svc = _make_svc(tmp_path)
        os.makedirs(svc.wal_dir)
        # Empty WAL dir
        process_files(svc)
        svc._insert_with_dedup.assert_not_called()

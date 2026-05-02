import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.loader import WALLoaderService

# These tests exercise WAL loading/archiving, not dedup logic.
# Explicitly disable dedup to isolate concerns.
pytestmark = pytest.mark.usefixtures("_disable_wal_dedup")


@pytest.fixture(autouse=False)
def _disable_wal_dedup(monkeypatch):
    monkeypatch.setenv("HFT_WAL_DEDUP_ENABLED", "0")


def test_wal_loader_processes_and_archives(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    row = {
        "symbol": "AAA",
        "exchange": "TSE",
        "type": "Tick",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price": 1.23,
        "volume": 2,
        "bids_price": [1.2],
        "bids_vol": [1],
        "asks_price": [1.3],
        "asks_vol": [2],
        "seq_no": 1,
    }

    fpath = wal_dir / "market_data_123.jsonl"
    fpath.write_text(json.dumps(row) + "\n")

    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    loader.process_files()

    archived = archive_dir / fpath.name
    assert archived.exists()
    assert not fpath.exists()
    loader.ch_client.insert.assert_called()


def test_wal_loader_skips_recent_file(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    fpath = wal_dir / "orders_999.jsonl"
    fpath.write_text("{}\n")
    os.utime(fpath, None)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.process_files()

    assert fpath.exists()
    assert not (archive_dir / fpath.name).exists()


def test_wal_loader_non_market_tables(tmp_path):
    """Test that orders and trades (fills) tables are properly inserted to ClickHouse.

    Phase 12 fix B1: Previously, non-market tables would fall through without
    insert logic, returning True but not actually inserting. Now they have
    proper insert logic with retry.
    """
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    # Use the same embedded timestamp for both files so this test remains
    # order-insensitive under strict-order replay.
    orders = wal_dir / "orders_1.jsonl"
    fills = wal_dir / "fills_1.jsonl"
    orders.write_text(json.dumps({"order_id": "O1", "symbol": "2330", "side": "Buy"}) + "\n")
    fills.write_text(json.dumps({"fill_id": "F1", "symbol": "2330", "price": 100.5}) + "\n")

    past = time.time() - 10
    os.utime(orders, (past, past))
    os.utime(fills, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    loader.process_files()

    assert (archive_dir / orders.name).exists()
    assert (archive_dir / fills.name).exists()
    # Phase 12: Now inserts are called for orders and fills tables
    assert loader.ch_client.insert.call_count == 2
    # Verify the table names
    call_args = [call[0][0] for call in loader.ch_client.insert.call_args_list]
    assert "hft.orders" in call_args
    assert "hft.fills" in call_args


def test_wal_loader_force_skips_mtime_check(tmp_path):
    """Test that process_files(force=True) skips mtime check.

    Phase 12 P2 feature: force flush at market close.
    """
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    row = {
        "symbol": "AAA",
        "exchange": "TSE",
        "type": "Tick",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price": 1.23,
        "volume": 2,
        "bids_price": [1.2],
        "bids_vol": [1],
        "asks_price": [1.3],
        "asks_vol": [2],
        "seq_no": 1,
    }

    fpath = wal_dir / "market_data_5.jsonl"
    fpath.write_text(json.dumps(row) + "\n")
    # Touch file just now (would normally be skipped)
    os.utime(fpath, None)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    # Without force, should skip
    loader.process_files(force=False)
    assert fpath.exists()
    loader.ch_client.insert.assert_not_called()

    # With force, should process
    loader.process_files(force=True)
    assert not fpath.exists()
    assert (archive_dir / fpath.name).exists()
    loader.ch_client.insert.assert_called()


def test_wal_loader_accumulation_check(tmp_path):
    """Test WAL accumulation monitoring.

    Phase 12 P2 feature: C5 WAL directory size monitoring.
    """
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    # Create some WAL files
    for i in range(5):
        fpath = wal_dir / f"market_data_{i}.jsonl"
        fpath.write_text('{"test": 1}\n' * 100)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.metrics = MagicMock()
    loader._last_wal_check_ts = 0  # Force check

    loader._check_wal_accumulation()

    # Verify metrics were updated
    loader.metrics.wal_file_count.set.assert_called()
    loader.metrics.wal_directory_size_bytes.set.assert_called()
    loader.metrics.wal_oldest_file_age_seconds.set.assert_called()
    loader.metrics.wal_drain_eta_seconds.set.assert_called()

    # Check file count
    call_args = loader.metrics.wal_file_count.set.call_args[0][0]
    assert call_args == 5


def test_insert_with_retry_records_success_no_retry_outcome(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()
    loader.metrics = MagicMock()

    ok = loader._insert_with_retry(
        "hft.market_data",
        ["symbol"],
        [["2330"]],
        "market_data",
        row_count=1,
    )

    assert ok is True
    loader.metrics.recorder_insert_batches_total.labels.assert_any_call(table="market_data", result="success_no_retry")


def test_insert_with_retry_records_success_after_retry_outcome(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()
    loader.ch_client.insert.side_effect = [RuntimeError("boom"), None]
    loader.metrics = MagicMock()
    loader._insert_max_retries = 2

    with patch("time.sleep", return_value=None):
        ok = loader._insert_with_retry(
            "hft.market_data",
            ["symbol"],
            [["2330"]],
            "market_data",
            row_count=1,
        )

    assert ok is True
    loader.metrics.recorder_insert_batches_total.labels.assert_any_call(
        table="market_data", result="success_after_retry"
    )
    loader.metrics.recorder_insert_retry_total.labels.assert_any_call(table="market_data", result="retry")
    loader.metrics.recorder_insert_retry_total.labels.assert_any_call(table="market_data", result="success")


def test_insert_with_retry_records_failed_no_client_outcome(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = None
    loader.metrics = MagicMock()

    ok = loader._insert_with_retry(
        "hft.market_data",
        ["symbol"],
        [["2330"]],
        "market_data",
        row_count=1,
    )

    assert ok is False
    loader.metrics.recorder_insert_batches_total.labels.assert_any_call(table="market_data", result="failed_no_client")


def test_parse_table_from_filename_handles_prefixes():
    assert WALLoaderService._parse_table_from_filename("hft.market_data_123.jsonl") == "market_data"
    assert WALLoaderService._parse_table_from_filename("market_data_123.jsonl") == "market_data"
    assert WALLoaderService._parse_table_from_filename("hft.orders_1.jsonl") == "orders"
    assert WALLoaderService._parse_table_from_filename("fills_2.jsonl") == "fills"
    assert WALLoaderService._parse_table_from_filename("latency_spans_9.jsonl") == "latency_spans"
    assert WALLoaderService._parse_table_from_filename("unknown_9.jsonl") == "unknown"


def test_manifest_load_removes_stuck_entries(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    stuck = wal_dir / "market_data_1.jsonl"
    stuck.write_text("{}\n")

    manifest_path = tmp_path / "manifest.txt"
    manifest_path.write_text("market_data_1.jsonl\nother_2.jsonl\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._manifest_enabled = True
    loader._manifest_path = str(manifest_path)

    loader._load_manifest()

    assert "market_data_1.jsonl" not in loader._manifest
    assert "other_2.jsonl" in loader._manifest

    loader._mark_processed(str(wal_dir / "new_3.jsonl"))
    loader._save_manifest()
    saved = manifest_path.read_text().splitlines()
    assert "other_2.jsonl" in saved
    assert "new_3.jsonl" in saved


def test_extract_file_ts_handles_invalid_names():
    assert WALLoaderService._extract_file_ts("market_data_123.jsonl") == 123
    assert WALLoaderService._extract_file_ts("batch_1775792429113485521_3813.jsonl") == 1775792429113485521
    assert WALLoaderService._extract_file_ts("market_data_bad.jsonl") == 0
    assert WALLoaderService._extract_file_ts("market_data.jsonl") == 0


def test_get_new_files_manifest_disabled_sorted(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    for ts in [5, 2, 9]:
        (wal_dir / f"market_data_{ts}.jsonl").write_text("{}\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._manifest_enabled = False

    files = loader._get_new_files()
    basenames = [os.path.basename(f) for f in files]
    assert basenames == ["market_data_2.jsonl", "market_data_5.jsonl", "market_data_9.jsonl"]


def test_get_new_files_manifest_oserror(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._manifest_enabled = True

    with patch("os.listdir", side_effect=OSError):
        assert loader._get_new_files() == []


def test_process_files_defers_when_no_client(tmp_path):
    """process_files() must not write to DLQ when ch_client is None (startup race fix)."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    row = {
        "symbol": "2330",
        "exchange": "TSE",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 100000,
        "volume": 1,
        "bids_price": [990000],
        "bids_vol": [10],
        "asks_price": [1000000],
        "asks_vol": [5],
        "seq_no": 1,
    }
    fpath = wal_dir / "market_data_100.jsonl"
    fpath.write_text(json.dumps(row) + "\n")
    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    # ch_client is None — simulates pre-connection state

    loader.process_files(force=True)

    # File must NOT have been moved or converted to DLQ
    assert fpath.exists(), "WAL file should remain for retry after client connects"
    dlq_dir = wal_dir / "dlq"
    assert not dlq_dir.exists() or not list(dlq_dir.iterdir()), "DLQ must be empty"


def test_replay_dlq_inserts_and_archives(tmp_path):
    """replay_dlq() should insert DLQ rows and move files to archive."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    dlq_dir = wal_dir / "dlq"
    wal_dir.mkdir()
    archive_dir.mkdir()
    dlq_dir.mkdir()

    row = {
        "symbol": "2330",
        "exchange": "TSE",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 100000,
        "volume": 1,
        "bids_price": [990000],
        "bids_vol": [10],
        "asks_price": [1000000],
        "asks_vol": [5],
        "seq_no": 1,
    }
    dlq_file = dlq_dir / "market_data_300.jsonl"
    with open(dlq_file, "w") as f:
        f.write(
            json.dumps(
                {
                    "_dlq_meta": True,
                    "table": "market_data",
                    "error": "insert_failed_after_retries",
                    "timestamp": 300,
                    "row_count": 1,
                }
            )
            + "\n"
        )
        f.write(json.dumps(row) + "\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    result = loader.replay_dlq()

    assert result["replayed"] == 1
    assert result["failed"] == 0
    assert not dlq_file.exists(), "DLQ file should be archived after success"
    loader.ch_client.insert.assert_called_once()


def test_replay_dlq_dry_run_does_not_move(tmp_path):
    """replay_dlq(dry_run=True) logs without inserting or moving files."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    dlq_dir = wal_dir / "dlq"
    wal_dir.mkdir()
    archive_dir.mkdir()
    dlq_dir.mkdir()

    row = {
        "symbol": "2330",
        "exchange": "TSE",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 100000,
        "volume": 1,
        "bids_price": [990000],
        "bids_vol": [10],
        "asks_price": [1000000],
        "asks_vol": [5],
        "seq_no": 1,
    }
    dlq_file = dlq_dir / "market_data_400.jsonl"
    with open(dlq_file, "w") as f:
        f.write(
            json.dumps({"_dlq_meta": True, "table": "market_data", "error": "test", "timestamp": 400, "row_count": 1})
            + "\n"
        )
        f.write(json.dumps(row) + "\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    result = loader.replay_dlq(dry_run=True)

    assert result["replayed"] == 1
    assert dlq_file.exists(), "dry_run must not move files"
    loader.ch_client.insert.assert_not_called()

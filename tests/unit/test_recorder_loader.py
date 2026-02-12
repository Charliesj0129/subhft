import json
import os
import time
from unittest.mock import MagicMock, patch

from hft_platform.recorder.loader import WALLoaderService


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

    orders = wal_dir / "orders_1.jsonl"
    fills = wal_dir / "fills_2.jsonl"
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
    # Phase 12: Now inserts are called for orders and trades tables
    assert loader.ch_client.insert.call_count == 2
    # Verify the table names
    call_args = [call[0][0] for call in loader.ch_client.insert.call_args_list]
    assert "hft.orders" in call_args
    assert "hft.trades" in call_args


def test_wal_loader_invalid_json_archives(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    fpath = wal_dir / "market_data_3.jsonl"
    fpath.write_text("{bad json}\n")
    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    loader.process_files()

    # Phase 5: Corrupt files now go to quarantine instead of archive
    corrupt_dir = wal_dir / "corrupt"
    assert corrupt_dir.exists()
    assert (corrupt_dir / fpath.name).exists()


def test_wal_loader_insert_failure_still_archives(tmp_path):
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

    fpath = wal_dir / "market_data_4.jsonl"
    fpath.write_text(json.dumps(row) + "\n")
    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()
    loader.ch_client.insert.side_effect = RuntimeError("boom")

    loader.process_files()

    # Phase 5: Failed inserts now go to DLQ after retry exhaustion
    # File should NOT be archived (stays for retry or DLQ written)
    dlq_dir = wal_dir / "dlq"
    assert dlq_dir.exists()
    dlq_files = list(dlq_dir.glob("market_data_*.jsonl"))
    assert len(dlq_files) == 1
    loader.ch_client.insert.assert_called()


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

    # Check file count
    call_args = loader.metrics.wal_file_count.set.call_args[0][0]
    assert call_args == 5


def test_parse_table_from_filename_handles_prefixes():
    assert WALLoaderService._parse_table_from_filename("hft.market_data_123.jsonl") == "market_data"
    assert WALLoaderService._parse_table_from_filename("market_data_123.jsonl") == "market_data"
    assert WALLoaderService._parse_table_from_filename("hft.orders_1.jsonl") == "orders"
    assert WALLoaderService._parse_table_from_filename("fills_2.jsonl") == "trades"
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


def test_cleanup_old_dlq_files_archives(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    dlq_dir = wal_dir / "dlq"
    dlq_dir.mkdir()

    dlq_file = dlq_dir / "market_data_1.jsonl"
    dlq_file.write_text("{}\n")
    past = time.time() - 10
    os.utime(dlq_file, (past, past))

    dlq_archive = tmp_path / "dlq_archive"

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._dlq_retention_days = 0
    loader._dlq_archive_path = str(dlq_archive)
    loader._last_dlq_cleanup_ts = 0

    loader._cleanup_old_dlq_files()

    assert not dlq_file.exists()
    assert (dlq_archive / dlq_file.name).exists()


def test_cleanup_old_corrupt_files(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    corrupt_dir = wal_dir / "corrupt"
    corrupt_dir.mkdir()

    corrupt_file = corrupt_dir / "market_data_1.jsonl"
    corrupt_file.write_text("{}\n")
    past = time.time() - 10
    os.utime(corrupt_file, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._corrupt_retention_days = 0
    loader._last_corrupt_cleanup_ts = 0

    loader._cleanup_old_corrupt_files()

    assert not corrupt_file.exists()


def test_write_to_dlq_writes_metadata(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    rows = [{"order_id": "O1"}, {"order_id": "O2"}]

    loader._write_to_dlq("orders", rows, "boom")

    dlq_dir = wal_dir / "dlq"
    files = list(dlq_dir.glob("orders_*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text().splitlines()
    header = json.loads(content[0])
    assert header["_dlq_meta"] is True
    assert header["table"] == "orders"
    assert header["error"] == "boom"
    assert header["row_count"] == 2


def test_dedup_skips_duplicate_and_archives(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
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

    fpath = wal_dir / "market_data_99.jsonl"
    fpath.write_text(json.dumps(row) + "\n")
    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._dedup_enabled = True
    loader._manifest_enabled = False
    loader.ch_client = MagicMock()
    loader.ch_client.command.return_value = "1"
    loader.insert_batch = MagicMock(return_value=True)

    loader.process_files(force=True)

    assert loader.insert_batch.call_count == 0
    assert (archive_dir / fpath.name).exists()


def test_dedup_records_hash_after_insert(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
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

    fpath = wal_dir / "market_data_100.jsonl"
    fpath.write_text(json.dumps(row) + "\n")
    past = time.time() - 10
    os.utime(fpath, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._dedup_enabled = True
    loader._manifest_enabled = False
    loader.ch_client = MagicMock()
    loader.ch_client.command.return_value = "0"
    loader.insert_batch = MagicMock(return_value=True)
    loader._record_dedup = MagicMock()

    loader.process_files(force=True)

    loader._record_dedup.assert_called_once()
    assert (archive_dir / fpath.name).exists()


def test_extract_file_ts_handles_invalid_names():
    assert WALLoaderService._extract_file_ts("market_data_123.jsonl") == 123
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


def test_process_single_file_strict_order_skips(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    fpath = wal_dir / "market_data_10.jsonl"
    fpath.write_text("{}\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._strict_order = True
    loader._last_processed_ts = 20

    processed = loader._process_single_file(str(fpath), force=True)
    assert processed is False
    assert fpath.exists()


def test_process_single_file_unknown_table_skips(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    fpath = wal_dir / "_1.jsonl"
    fpath.write_text("{}\n")

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    processed = loader._process_single_file(str(fpath), force=True)

    assert processed is False
    assert fpath.exists()


def test_compute_backoff_bounds():
    loader = WALLoaderService(wal_dir=".wal", archive_dir=".wal/archive")

    with patch("random.random", return_value=0.0):
        delay = loader._compute_connect_backoff(0)
        assert delay >= 1.0

    with patch("random.random", return_value=1.0):
        delay = loader._compute_insert_backoff(2)
        assert delay >= 0.1

import json
import os
import time
from unittest.mock import MagicMock

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

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
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    orders = wal_dir / "orders_1.jsonl"
    fills = wal_dir / "fills_2.jsonl"
    orders.write_text(json.dumps({"order_id": "O1"}) + "\n")
    fills.write_text(json.dumps({"fill_id": "F1"}) + "\n")

    past = time.time() - 10
    os.utime(orders, (past, past))
    os.utime(fills, (past, past))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    loader.process_files()

    assert (archive_dir / orders.name).exists()
    assert (archive_dir / fills.name).exists()
    loader.ch_client.insert.assert_not_called()


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

    assert (archive_dir / fpath.name).exists()


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

    assert (archive_dir / fpath.name).exists()
    loader.ch_client.insert.assert_called()

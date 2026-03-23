"""Edge cases for WALLoaderService: corrupt files, partial writes, error paths, cleanup."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.loader import WALLoaderService

# These tests exercise WAL edge cases, not dedup logic.
pytestmark = pytest.mark.usefixtures("_disable_wal_dedup")


@pytest.fixture(autouse=False)
def _disable_wal_dedup(monkeypatch):
    monkeypatch.setenv("HFT_WAL_DEDUP_ENABLED", "0")

# Shared helpers (local copies — not in conftest)


def _make_wal_file(wal_dir, filename, row, age_s=10):
    """Helper: write a single-row WAL file with mtime in the past."""
    fpath = wal_dir / filename
    fpath.write_text(json.dumps(row) + "\n")
    past = time.time() - age_s
    os.utime(fpath, (past, past))
    return fpath


def _run_and_capture_warnings(loader):
    """Run process_files() and return structlog warning events via patching."""
    import hft_platform.recorder.loader as loader_mod

    captured = []
    original = loader_mod.logger.warning

    def capturing_warn(event, **kw):
        captured.append({"event": event, **kw})
        return original(event, **kw)

    loader_mod.logger.warning = capturing_warn
    try:
        loader.process_files()
    finally:
        loader_mod.logger.warning = original
    return captured


# Error path: corrupt / invalid JSON


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


# Error path: insert failure → DLQ


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


def test_insert_with_retry_records_failed_after_retry_outcome(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()
    loader.ch_client.insert.side_effect = RuntimeError("boom")
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

    assert ok is False
    loader.metrics.recorder_insert_batches_total.labels.assert_any_call(
        table="market_data", result="failed_after_retry"
    )
    loader.metrics.recorder_insert_retry_total.labels.assert_any_call(table="market_data", result="failed")


# DLQ write and cleanup


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


# Corrupt file cleanup


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


# Archive cleanup (retention policy)


def test_cleanup_old_archive_files(tmp_path):
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    old_archive = archive_dir / "market_data_1.jsonl"
    old_archive.write_text("{}\n")
    past = time.time() - 10
    os.utime(old_archive, (past, past))

    new_archive = archive_dir / "market_data_2.jsonl"
    new_archive.write_text("{}\n")
    future = time.time() + 60
    os.utime(new_archive, (future, future))

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._archive_retention_days = 0
    loader._last_archive_cleanup_ts = 0
    loader._cleanup_old_archive_files()

    assert not old_archive.exists()
    assert new_archive.exists()


# Dedup paths


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


# Strict-order and unknown-table skips


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


# Orderbook warning edge cases


def test_tick_type_no_orderbook_warning(tmp_path):
    """Tick-type rows must not trigger 'Missing orderbook side' warning."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    tick_row = {
        "symbol": "TXFC6",
        "exchange": "FUT",
        "type": "Tick",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 330000000,
        "volume": 2,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
    }
    _make_wal_file(wal_dir, "market_data_200.jsonl", tick_row)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    warnings = _run_and_capture_warnings(loader)
    assert not any("Missing orderbook side" in w.get("event", "") for w in warnings)


def test_tick_type_lowercase_no_orderbook_warning(tmp_path):
    """Lowercase tick-type rows must not trigger orderbook-side warning."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    tick_row = {
        "symbol": "TXFC6",
        "exchange": "FUT",
        "type": "tick",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 330000000,
        "volume": 2,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
    }
    _make_wal_file(wal_dir, "market_data_201.jsonl", tick_row)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    warnings = _run_and_capture_warnings(loader)
    assert not any("Missing orderbook side" in w.get("event", "") for w in warnings)


def test_bidask_both_empty_no_orderbook_warning(tmp_path):
    """BidAsk rows with BOTH sides empty (book-cleared at close) must not warn."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    row = {
        "symbol": "TMFC6",
        "exchange": "FUT",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 0,
        "volume": 0,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
    }
    _make_wal_file(wal_dir, "market_data_202.jsonl", row)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    warnings = _run_and_capture_warnings(loader)
    assert not any("Missing orderbook side" in w.get("event", "") for w in warnings)


def test_bidask_one_side_empty_warns(tmp_path):
    """BidAsk rows with exactly ONE side empty must still warn (genuine gap)."""
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    row = {
        "symbol": "TXO32000O6",
        "exchange": "FUT",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 0,
        "volume": 0,
        "bids_price": [320000000],
        "bids_vol": [5],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
    }
    _make_wal_file(wal_dir, "market_data_203.jsonl", row)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = MagicMock()

    warnings = _run_and_capture_warnings(loader)
    assert any("Missing orderbook side" in w.get("event", "") for w in warnings)


# Backoff computation bounds


def test_compute_backoff_bounds(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = wal_dir / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir(parents=True, exist_ok=True)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))

    with patch("random.random", return_value=0.0):
        delay = loader._compute_connect_backoff(0)
        assert delay >= 1.0

    with patch("random.random", return_value=1.0):
        delay = loader._compute_insert_backoff(2)
        assert delay >= 0.1

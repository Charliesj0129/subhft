"""CE3-04: Full replay safety contract tests (ordering + dedup + manifest restart/crash)."""

from __future__ import annotations

import json
import os
import time
import types
from unittest.mock import MagicMock, patch

from hft_platform.recorder.loader import WALLoaderService


def _write_market_file(path, seq: int, ts_suffix: int):
    row = {
        "symbol": "2330",
        "exchange": "TSE",
        "type": "Tick",
        "exch_ts": seq,
        "ingest_ts": seq,
        "price": 100.0,
        "volume": 1,
        "bids_price": [99.0],
        "bids_vol": [1],
        "asks_price": [101.0],
        "asks_vol": [1],
        "seq_no": seq,
    }
    fpath = path / f"market_data_{ts_suffix}.jsonl"
    fpath.write_text(json.dumps(row) + "\n", encoding="utf-8")
    past = time.time() - 10
    os.utime(fpath, (past, past))
    return fpath


def test_replay_strict_ordering_skips_out_of_order_file(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()
    newer = _write_market_file(wal_dir, seq=2, ts_suffix=200)
    older = _write_market_file(wal_dir, seq=1, ts_suffix=100)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader._strict_order = True
    loader._manifest_enabled = False
    loader.insert_batch = types.MethodType(lambda self, table, rows: True, loader)

    assert loader._process_single_file(str(newer), force=True) is True
    # Out-of-order older file should be skipped in strict mode.
    assert loader._process_single_file(str(older), force=True) is False
    assert older.exists()
    assert (archive_dir / newer.name).exists()


def test_replay_dedup_prevents_duplicate_insert_after_crash_before_archive(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()
    fpath = _write_market_file(wal_dir, seq=1, ts_suffix=123)

    loader1 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader1._dedup_enabled = True
    loader1.ch_client = MagicMock()
    insert_calls_1 = {"n": 0}

    def _insert_batch_ok(self, table, rows):
        insert_calls_1["n"] += 1
        return True

    loader1.insert_batch = types.MethodType(_insert_batch_ok, loader1)
    loader1._is_duplicate = types.MethodType(lambda self, table, content_hash: False, loader1)
    loader1._record_dedup = types.MethodType(lambda self, table, content_hash, row_count: None, loader1)

    def _boom_once(src, dst):
        raise RuntimeError("simulated crash after insert before archive")

    with patch("shutil.move", side_effect=_boom_once):
        with patch("hft_platform.recorder.loader.logger"):
            try:
                loader1._process_single_file(str(fpath), force=True)
            except RuntimeError:
                pass
    assert insert_calls_1["n"] == 1
    assert fpath.exists()

    # Restart loader; dedup guard should detect previously inserted content and avoid re-insert.
    loader2 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader2._dedup_enabled = True
    loader2.ch_client = MagicMock()
    insert_calls_2 = {"n": 0}
    loader2.insert_batch = types.MethodType(
        lambda self, table, rows: insert_calls_2.__setitem__("n", insert_calls_2["n"] + 1) or True, loader2
    )
    loader2._is_duplicate = types.MethodType(lambda self, table, content_hash: True, loader2)
    loader2._record_dedup = types.MethodType(lambda self, table, content_hash, row_count: None, loader2)
    assert loader2._process_single_file(str(fpath), force=True) is True
    assert insert_calls_2["n"] == 0
    assert (archive_dir / fpath.name).exists()


def test_replay_manifest_restart_reprocesses_stuck_manifest_entry(tmp_path, monkeypatch):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()
    manifest_path = tmp_path / "manifest.txt"
    monkeypatch.setenv("HFT_WAL_MANIFEST_PATH", str(manifest_path))

    # First run processes file and writes manifest.
    fpath = _write_market_file(wal_dir, seq=1, ts_suffix=321)
    loader1 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader1.ch_client = object()
    loader1.insert_batch = types.MethodType(lambda self, table, rows: True, loader1)
    loader1.process_files(force=True)
    assert (archive_dir / fpath.name).exists()
    assert manifest_path.exists()

    # Simulate crash/restore: same filename appears again in WAL while manifest still marks it processed.
    restored = _write_market_file(wal_dir, seq=2, ts_suffix=321)
    loader2 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader2.ch_client = object()
    loader2.insert_batch = types.MethodType(lambda self, table, rows: True, loader2)
    loader2._load_manifest()
    # _load_manifest should remove stuck entries still present in wal_dir.
    assert restored.name not in loader2._manifest
    loader2.process_files(force=True)
    assert (archive_dir / restored.name).exists()

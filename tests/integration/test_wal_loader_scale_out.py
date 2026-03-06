"""CE3-03: Scale-out WAL loader workers with shard-claim protocol."""

from __future__ import annotations

import json
import os
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

from hft_platform.recorder.loader import WALLoaderService


def test_two_loaders_no_duplicate_inserts_with_shard_claim(tmp_path):
    wal_dir = tmp_path / "wal"
    archive_dir = tmp_path / "archive"
    wal_dir.mkdir()
    archive_dir.mkdir()

    # Create 8 WAL files with deterministic row markers.
    for i in range(8):
        row = {
            "symbol": "2330",
            "exchange": "TSE",
            "type": "Tick",
            "exch_ts": i + 1,
            "ingest_ts": i + 1,
            "price": 100.0 + i,
            "volume": 1,
            "bids_price": [100.0],
            "bids_vol": [1],
            "asks_price": [101.0],
            "asks_vol": [1],
            "seq_no": i,
        }
        fpath = wal_dir / f"market_data_{1000 + i}.jsonl"
        fpath.write_text(json.dumps(row) + "\n", encoding="utf-8")
        past = time.time() - 10
        os.utime(fpath, (past, past))

    l1 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    l2 = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    l1._loader_concurrency = 1
    l2._loader_concurrency = 1
    l1._manifest_enabled = False
    l2._manifest_enabled = False
    # process_files() now defers until client is ready (startup race guard).
    l1.ch_client = object()
    l2.ch_client = object()

    inserted_seq: list[int] = []
    ins_lock = threading.Lock()

    def _insert_batch(self, table: str, rows: list) -> bool:
        # Sleep a little to increase interleaving/race window.
        time.sleep(0.005)
        with ins_lock:
            inserted_seq.extend(int(r["seq_no"]) for r in rows if "seq_no" in r)
        return True

    l1.insert_batch = types.MethodType(_insert_batch, l1)
    l2.insert_batch = types.MethodType(_insert_batch, l2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(l1.process_files, True)
        f2 = pool.submit(l2.process_files, True)
        f1.result(timeout=5)
        f2.result(timeout=5)

    assert sorted(inserted_seq) == list(range(8))
    assert len(inserted_seq) == 8  # no duplicate inserts
    assert not list(wal_dir.glob("*.jsonl"))
    assert len(list(archive_dir.glob("*.jsonl"))) == 8

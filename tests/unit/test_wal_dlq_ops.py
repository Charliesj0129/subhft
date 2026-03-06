from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "wal_dlq_ops.py"
    spec = importlib.util.spec_from_file_location("wal_dlq_ops", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_dlq_file(path: Path, table: str = "market_data", *, ts: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"_dlq_meta": True, "table": table, "error": "test", "timestamp": ts, "row_count": 1},
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "symbol": "2330",
                    "exchange": "TSE",
                    "type": "BidAsk",
                    "exch_ts": ts,
                    "ingest_ts": ts,
                    "price_scaled": 100000,
                    "volume": 1,
                    "bids_price": [990000],
                    "bids_vol": [10],
                    "asks_price": [1000000],
                    "asks_vol": [5],
                    "seq_no": ts,
                }
            )
            + "\n"
        )


def test_parser_supports_commands():
    mod = _load_module()
    parser = mod._build_parser()

    s = parser.parse_args(["status"])
    assert s.command == "status"

    r = parser.parse_args(["replay", "--dry-run", "--max-files", "10"])
    assert r.command == "replay"
    assert r.dry_run is True
    assert r.max_files == 10

    c = parser.parse_args(["cleanup-tmp", "--dry-run"])
    assert c.command == "cleanup-tmp"
    assert c.dry_run is True


def test_status_warns_when_dlq_files_exist(tmp_path: Path):
    mod = _load_module()
    dlq_file = tmp_path / ".wal" / "dlq" / "market_data_1.jsonl"
    _seed_dlq_file(dlq_file, ts=1)

    rc = mod.main(["status", "--wal-dir", str(tmp_path / ".wal"), "--output-dir", str(tmp_path / "out")])
    assert rc == 1

    reports = list((tmp_path / "out" / "status").glob("dlq_status_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["overall"] == "warn"
    assert payload["result"]["dlq"]["files"] == 1


def test_replay_dry_run_returns_warn_and_keeps_file(tmp_path: Path):
    mod = _load_module()
    wal_dir = tmp_path / ".wal"
    archive_dir = wal_dir / "archive"
    dlq_file = wal_dir / "dlq" / "market_data_2.jsonl"
    archive_dir.mkdir(parents=True, exist_ok=True)
    _seed_dlq_file(dlq_file, ts=2)

    rc = mod.main(
        [
            "replay",
            "--wal-dir",
            str(wal_dir),
            "--archive-dir",
            str(archive_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--dry-run",
            "--allow-warn-exit-zero",
        ]
    )
    assert rc == 0
    assert dlq_file.exists()


def test_replay_dry_run_does_not_require_loader_import(tmp_path: Path, monkeypatch):
    mod = _load_module()
    wal_dir = tmp_path / ".wal"
    archive_dir = wal_dir / "archive"
    dlq_file = wal_dir / "dlq" / "market_data_3.jsonl"
    archive_dir.mkdir(parents=True, exist_ok=True)
    _seed_dlq_file(dlq_file, ts=3)

    def _boom():
        raise ModuleNotFoundError("clickhouse_connect")

    monkeypatch.setattr(mod, "_load_loader_cls", _boom)

    rc = mod.main(
        [
            "replay",
            "--wal-dir",
            str(wal_dir),
            "--archive-dir",
            str(archive_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--dry-run",
            "--allow-warn-exit-zero",
        ]
    )
    assert rc == 0
    assert dlq_file.exists()


def test_replay_max_files_processes_partial(tmp_path: Path):
    from hft_platform.recorder.loader import WALLoaderService

    wal_dir = tmp_path / ".wal"
    archive_dir = wal_dir / "archive"
    dlq_dir = wal_dir / "dlq"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dlq_dir.mkdir(parents=True, exist_ok=True)

    _seed_dlq_file(dlq_dir / "market_data_1.jsonl", ts=1)
    _seed_dlq_file(dlq_dir / "market_data_2.jsonl", ts=2)

    loader = WALLoaderService(wal_dir=str(wal_dir), archive_dir=str(archive_dir))
    loader.ch_client = object()
    loader.insert_batch = lambda table, rows: True  # type: ignore[method-assign]

    out = loader.replay_dlq(max_files=1)
    assert out["selected"] == 1
    assert out["replayed"] == 1
    assert len(list(dlq_dir.glob("*.jsonl"))) == 1
    assert len(list(archive_dir.glob("market_data_*.jsonl"))) == 1


def test_cleanup_tmp_deletes_old_files(tmp_path: Path):
    mod = _load_module()
    wal_dir = tmp_path / ".wal"
    wal_dir.mkdir()

    old_tmp = wal_dir / "old.tmp"
    old_tmp.write_text("x", encoding="utf-8")
    past = time.time() - 600
    os.utime(old_tmp, (past, past))

    new_tmp = wal_dir / "new.tmp"
    new_tmp.write_text("x", encoding="utf-8")
    now = time.time()
    os.utime(new_tmp, (now, now))

    rc = mod.main(
        [
            "cleanup-tmp",
            "--wal-dir",
            str(wal_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--min-age-seconds",
            "300",
        ]
    )
    assert rc == 0
    assert not old_tmp.exists()
    assert new_tmp.exists()

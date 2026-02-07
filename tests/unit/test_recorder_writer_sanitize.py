import pytest

from hft_platform.recorder import writer as writer_module
from hft_platform.recorder.writer import DataWriter


def test_sanitize_timestamps_drops_future_rows_and_fixes_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_TS_MAX_FUTURE_S", "1")
    writer = DataWriter(wal_dir=str(tmp_path))

    now_ns = 1_700_000_000_000_000_000
    monkeypatch.setattr(writer_module.time, "time_ns", lambda: now_ns)

    data = [
        {"exch_ts": now_ns + 2_000_000_000, "ingest_ts": now_ns},
        {"exch_ts": now_ns, "ingest_ts": now_ns + 3_000_000_000},
        {"exch_ts": now_ns, "ingest_ts": now_ns - 1},
        {"exch_ts": None, "ingest_ts": None},
    ]

    kept = writer._sanitize_timestamps("hft.market_data", data)

    assert len(kept) == 2
    assert kept[0]["ingest_ts"] == now_ns
    assert kept[1]["exch_ts"] is None


def test_sanitize_timestamps_no_future_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_TS_MAX_FUTURE_S", "0")
    writer = DataWriter(wal_dir=str(tmp_path))

    data = [
        {"exch_ts": 200, "ingest_ts": 100},
        {"exch_ts": 0, "ingest_ts": 50},
    ]

    kept = writer._sanitize_timestamps("hft.market_data", data)

    assert kept[0]["ingest_ts"] == 200
    assert kept[1]["ingest_ts"] == 50


def test_compute_backoff_delay_with_jitter(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    monkeypatch.setattr(writer_module.random, "random", lambda: 0.0)

    delay = writer._compute_backoff_delay(2)

    assert 1.9 <= delay <= 2.1

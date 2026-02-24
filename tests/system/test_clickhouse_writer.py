import os
import time

import clickhouse_connect
import pytest

from hft_platform.recorder.writer import DataWriter


def _ck_auth_kwargs() -> dict:
    username = (
        os.getenv("HFT_CLICKHOUSE_USER")
        or os.getenv("HFT_CLICKHOUSE_USERNAME")
        or os.getenv("CLICKHOUSE_USER")
        or os.getenv("CLICKHOUSE_USERNAME")
        or "default"
    )
    password = os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or ""
    return {"username": username, "password": password}


def _wait_for_clickhouse(host: str, port: int, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            client = clickhouse_connect.get_client(host=host, port=port, **_ck_auth_kwargs())
            client.command("SELECT 1")
            return True
        except Exception:
            time.sleep(0.5)
    return False


@pytest.mark.system
@pytest.mark.asyncio
async def test_clickhouse_writer_roundtrip(tmp_path, monkeypatch):
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    if not _wait_for_clickhouse(host, port):
        pytest.skip("ClickHouse not reachable")

    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "1")
    monkeypatch.setenv("HFT_CLICKHOUSE_HOST", host)
    monkeypatch.setenv("HFT_CLICKHOUSE_PORT", str(port))
    auth = _ck_auth_kwargs()
    monkeypatch.setenv("HFT_CLICKHOUSE_USER", str(auth["username"]))
    if auth["password"]:
        monkeypatch.setenv("HFT_CLICKHOUSE_PASSWORD", str(auth["password"]))

    writer = DataWriter(ch_host=host, ch_port=port, wal_dir=str(tmp_path))
    writer.connect()

    ingest_ts = int(time.time_ns())
    # Use scaled Int64 format (price * 1_000_000)
    row = {
        "symbol": "CH_TEST",
        "exchange": "TSE",
        "type": "Tick",
        "exch_ts": ingest_ts,
        "ingest_ts": ingest_ts,
        "price_scaled": 1_000_000,  # 1.0 * 1_000_000
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
    }

    await writer.write("hft.market_data", [row])

    client = clickhouse_connect.get_client(host=host, port=port, **auth)
    result = client.query(
        "SELECT count() FROM hft.market_data WHERE symbol='CH_TEST' AND ingest_ts=%(ts)s",
        parameters={"ts": ingest_ts},
    )
    assert result.result_rows[0][0] >= 1

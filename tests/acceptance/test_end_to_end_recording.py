import asyncio
import os
import time

import clickhouse_connect
import pytest

from hft_platform.events import MetaData, TickEvent
from hft_platform.services.system import HFTSystem


def _wait_for_clickhouse(host: str, port: int, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            client = clickhouse_connect.get_client(host=host, port=port)
            client.command("SELECT 1")
            return True
        except Exception:
            time.sleep(0.5)
    return False


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_end_to_end_recording(tmp_path, monkeypatch):
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    if not _wait_for_clickhouse(host, port):
        pytest.skip("ClickHouse not reachable")

    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'ACPT'\n    exchange: 'TSE'\n    price_scale: 10000\n")

    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "1")
    monkeypatch.setenv("HFT_CLICKHOUSE_HOST", host)
    monkeypatch.setenv("HFT_CLICKHOUSE_PORT", str(port))

    settings = {
        "paths": {
            "symbols": str(symbols_cfg),
            "strategy_limits": "config/base/strategy_limits.yaml",
            "order_adapter": "config/base/order_adapter.yaml",
        }
    }
    system = HFTSystem(settings)

    recorder_task = asyncio.create_task(system.recorder.run())
    bridge_task = asyncio.create_task(system._recorder_bridge())

    ingest_ts = time.time_ns()
    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=ingest_ts, local_ts=ingest_ts),
        symbol="ACPT",
        price=123450,
        volume=2,
        total_volume=2,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )

    await system.bus.publish(event)
    await asyncio.sleep(0.5)

    recorder_task.cancel()
    bridge_task.cancel()
    await asyncio.gather(recorder_task, bridge_task, return_exceptions=True)

    client = clickhouse_connect.get_client(host=host, port=port)
    result = client.query(
        "SELECT count() FROM hft.market_data WHERE symbol='ACPT' AND ingest_ts=%(ts)s",
        parameters={"ts": ingest_ts},
    )
    assert result.result_rows[0][0] >= 1

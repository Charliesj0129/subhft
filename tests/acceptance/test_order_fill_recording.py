import asyncio
import os
import time

import clickhouse_connect
import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
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
async def test_order_and_fill_recording(tmp_path, monkeypatch):
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    if not _wait_for_clickhouse(host, port):
        pytest.skip("ClickHouse not reachable")

    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'REC'\n    exchange: 'TSE'\n    price_scale: 10000\n")

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
    order = OrderEvent(
        order_id="ORD-1",
        strategy_id="S1",
        symbol="REC",
        status=OrderStatus.SUBMITTED,
        submitted_qty=3,
        filled_qty=0,
        remaining_qty=3,
        price=123450,
        side=Side.BUY,
        ingest_ts_ns=ingest_ts,
        broker_ts_ns=ingest_ts,
    )

    fill = FillEvent(
        fill_id="FILL-1",
        account_id="ACC",
        order_id="ORD-1",
        strategy_id="S1",
        symbol="REC",
        side=Side.BUY,
        qty=3,
        price=123450,
        fee=50,
        tax=0,
        ingest_ts_ns=ingest_ts,
        match_ts_ns=ingest_ts,
    )

    await system.bus.publish(order)
    await system.bus.publish(fill)
    await asyncio.sleep(0.5)

    recorder_task.cancel()
    bridge_task.cancel()
    await asyncio.gather(recorder_task, bridge_task, return_exceptions=True)

    client = clickhouse_connect.get_client(host=host, port=port)
    order_count = client.query(
        "SELECT count() FROM hft.orders WHERE order_id='ORD-1' AND strategy_id='S1'"
    ).result_rows[0][0]
    fill_count = client.query("SELECT count() FROM hft.trades WHERE fill_id='FILL-1' AND strategy_id='S1'").result_rows[
        0
    ][0]

    assert order_count >= 1
    assert fill_count >= 1

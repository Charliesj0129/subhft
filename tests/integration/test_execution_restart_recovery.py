import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.core import timebase
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter
from hft_platform.order.adapter import OrderAdapter


def _make_adapter(tmp_path, persist_path: str) -> OrderAdapter:
    cfg = tmp_path / "order_adapter.yaml"
    cfg.write_text(
        "\n".join(
            [
                "rate_limits:",
                "  shioaji_soft_cap: 1000",
                "  shioaji_hard_cap: 2000",
                "  window_seconds: 10",
            ]
        )
        + "\n"
    )
    with patch.dict(
        os.environ,
        {
            "HFT_ORDER_ID_MAP_PERSIST_PATH": persist_path,
            "HFT_ORDER_ID_MAP_PERSIST_INTERVAL_S": "0",
        },
    ):
        return OrderAdapter(
            config_path=str(cfg),
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )


def _make_router(persist_path: str, order_id_map: dict[str, str]) -> tuple[ExecutionRouter, MagicMock]:
    bus = MagicMock()
    position_store = MagicMock()
    position_store.positions = {}
    position_store.on_fill.return_value = MagicMock(realized_pnl=0)
    with patch.dict(
        os.environ,
        {
            "HFT_FILL_DEDUP_PERSIST_PATH": persist_path,
            "HFT_FILL_DEDUP_PERSIST_INTERVAL_S": "0",
        },
    ):
        router = ExecutionRouter(
            bus=bus,
            raw_queue=asyncio.Queue(),
            order_id_map=order_id_map,
            position_store=position_store,
            terminal_handler=MagicMock(),
        )
    return router, position_store


def _make_fill_raw(*, order_id: str = "ORD_RESTART", fill_id: str = "FILL_RESTART") -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": order_id,
            "code": "TXFD6",
            "action": "Buy",
            "price": 123.0,
            "quantity": 1,
            "seqno": fill_id,
            "account_id": "FUTACC1",
            "ts": timebase.now_ns(),
        },
        ingest_ts_ns=timebase.now_ns(),
    )


@pytest.mark.asyncio
async def test_restart_restores_order_id_routing_for_fill_resolution(tmp_path):
    persist_path = str(tmp_path / "order_id_map.jsonl")

    adapter1 = _make_adapter(tmp_path, persist_path)
    await adapter1._register_broker_ids("R47:101", {"ordno": "ORD_RESTART", "seqno": "SEQ_RESTART"})

    adapter2 = _make_adapter(tmp_path, persist_path)
    router2, _ = _make_router(str(tmp_path / "fill_dedup.jsonl"), adapter2.order_id_map)

    fill = router2.normalizer.normalize_fill(_make_fill_raw())

    assert fill is not None
    assert fill.strategy_id == "R47"


@pytest.mark.asyncio
async def test_restart_restores_fill_dedup_and_skips_replayed_fill(tmp_path):
    order_id_map = {"ORD_RESTART": "R47:101"}
    persist_path = str(tmp_path / "fill_dedup.jsonl")

    router1, position_store1 = _make_router(persist_path, dict(order_id_map))
    router1.raw_queue.put_nowait(_make_fill_raw())
    drained1 = await router1.stop(drain_timeout_s=1.0)

    assert drained1 == 1
    position_store1.on_fill.assert_called_once()
    assert os.path.exists(persist_path)

    router2, position_store2 = _make_router(persist_path, dict(order_id_map))
    router2.raw_queue.put_nowait(_make_fill_raw())
    drained2 = await router2.stop(drain_timeout_s=1.0)

    assert drained2 == 0
    position_store2.on_fill.assert_not_called()

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, PositionDelta, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.engine import RiskEngine
from hft_platform.services.execution import ExecutionService


async def _wait_for(predicate, timeout=1.0, step=0.01):
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("Timed out waiting for condition")


async def _collect(bus, count, timeout=1.0):
    events = []

    async def _consume():
        async for event in bus.consume(start_cursor=-1):
            events.append(event)
            if len(events) >= count:
                break

    await asyncio.wait_for(_consume(), timeout=timeout)
    return events


@pytest.mark.asyncio
async def test_risk_to_execution_pipeline(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    risk_cfg = tmp_path / "risk.yaml"
    risk_cfg.write_text(
        "\n".join(
            [
                "global_defaults:",
                "  max_notional: 1000000",
                "  max_price_cap: 100000",
                "storm_guard:",
                "  warm_threshold: -1000000",
                "  storm_threshold: -2000000",
                "  halt_threshold: -3000000",
            ]
        )
        + "\n"
    )

    adapter_cfg = tmp_path / "order_adapter.yaml"
    adapter_cfg.write_text(
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

    bus = RingBufferBus()
    intent_q = asyncio.Queue()
    order_q = asyncio.Queue()
    raw_exec_q = asyncio.Queue()

    order_id_map = {}
    client = MagicMock()
    client.get_exchange.return_value = "TSE"
    client.place_order.return_value = {"seq_no": "S1", "ord_no": "O1"}

    order_adapter = OrderAdapter(str(adapter_cfg), order_q, client, order_id_map)
    risk_engine = RiskEngine(str(risk_cfg), intent_q, order_q)
    pos_store = PositionStore()
    exec_service = ExecutionService(bus, raw_exec_q, order_id_map, pos_store, order_adapter)

    tasks = [
        asyncio.create_task(risk_engine.run()),
        asyncio.create_task(order_adapter.run()),
        asyncio.create_task(exec_service.run()),
    ]

    try:
        intent = OrderIntent(
            intent_id=1,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=10000,
            qty=2,
            tif=TIF.LIMIT,
            timestamp_ns=time.time_ns(),
        )
        await intent_q.put(intent)
        await asyncio.wait_for(intent_q.join(), timeout=1.0)

        await _wait_for(lambda: client.place_order.called, timeout=1.0)
        assert order_id_map["O1"] == "strat:1"

        raw_order = RawExecEvent(
            "order",
            {
                "ord_no": "O1",
                "status": {"status": "Filled"},
                "contract": {"code": "AAA"},
                "order": {"action": "Buy", "price": 1.0, "quantity": 2},
            },
            time.time_ns(),
        )
        raw_fill = RawExecEvent(
            "deal",
            {
                "seq_no": "F1",
                "ord_no": "O1",
                "code": "AAA",
                "action": "Buy",
                "quantity": 2,
                "price": 1.0,
                "ts": time.time_ns(),
            },
            time.time_ns(),
        )
        await raw_exec_q.put(raw_order)
        await raw_exec_q.put(raw_fill)
        await asyncio.wait_for(raw_exec_q.join(), timeout=1.0)

        events = await _collect(bus, 3, timeout=1.0)
        order_events = [e for e in events if isinstance(e, OrderEvent)]
        fill_events = [e for e in events if isinstance(e, FillEvent)]
        deltas = [e for e in events if isinstance(e, PositionDelta)]

        assert order_events and fill_events and deltas
        assert order_events[0].status == OrderStatus.FILLED
        assert fill_events[0].strategy_id == "strat"
        assert deltas[0].net_qty == 2

        await _wait_for(lambda: not order_adapter.live_orders, timeout=1.0)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_risk_rejects_and_blocks_order(tmp_path):
    risk_cfg = tmp_path / "risk.yaml"
    risk_cfg.write_text(
        "\n".join(
            [
                "global_defaults:",
                "  max_notional: 1",
                "  max_price_cap: 100000",
            ]
        )
        + "\n"
    )

    intent_q = asyncio.Queue()
    order_q = asyncio.Queue()
    engine = RiskEngine(str(risk_cfg), intent_q, order_q)

    task = asyncio.create_task(engine.run())
    try:
        intent = OrderIntent(
            intent_id=1,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=10000,
            qty=10,
            tif=TIF.LIMIT,
            timestamp_ns=time.time_ns(),
        )
        await intent_q.put(intent)
        await asyncio.wait_for(intent_q.join(), timeout=1.0)
        assert order_q.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_risk_respects_symbol_scale(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    risk_cfg = tmp_path / "risk.yaml"
    risk_cfg.write_text(
        "\n".join(
            [
                "global_defaults:",
                "  max_notional: 2.0",
                "  max_price_cap: 1.0",
            ]
        )
        + "\n"
    )

    intent_q = asyncio.Queue()
    order_q = asyncio.Queue()
    engine = RiskEngine(str(risk_cfg), intent_q, order_q)

    task = asyncio.create_task(engine.run())
    try:
        intent = OrderIntent(
            intent_id=1,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=150,  # 1.5 with scale=100
            qty=2,
            tif=TIF.LIMIT,
            timestamp_ns=time.time_ns(),
        )
        await intent_q.put(intent)
        await asyncio.wait_for(intent_q.join(), timeout=1.0)
        assert order_q.empty()

        ok_intent = OrderIntent(
            intent_id=2,
            strategy_id="strat",
            symbol="AAA",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=100,  # 1.0 with scale=100
            qty=2,
            tif=TIF.LIMIT,
            timestamp_ns=time.time_ns(),
        )
        await intent_q.put(ok_intent)
        await asyncio.wait_for(intent_q.join(), timeout=1.0)
        cmd = await asyncio.wait_for(order_q.get(), timeout=1.0)
        assert cmd.intent.intent_id == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

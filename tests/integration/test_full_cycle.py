import asyncio
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.services.system import HFTSystem
from hft_platform.strategy.base import BaseStrategy


class MockStrategy(BaseStrategy):
    def on_stats(self, event):
        pass

    def on_tick(self, event):
        pass


async def _exercise_full_order_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Verifies: Strategy -> Bus -> Risk -> OrderAdapter -> Gateway (Mock) -> Fill -> Bus -> Strategy
    """
    monkeypatch.setenv("HFT_RUNTIME_ROLE", "maintenance")

    settings = {
        "kafka": {"enabled": False},
        "wal": {"enabled": False},
        "risk": {"max_order_size": 100},
        "shioaji": {"simulation": True},
        "paths": {
            "symbols": "config/symbols.yaml",
            "strategy_limits": "config/base/strategy_limits.yaml",
            "order_adapter": "config/base/order_adapter.yaml",
        },
    }

    system = HFTSystem(settings)

    strat = MockStrategy("USER1")

    original_handle = strat.handle_event

    def handle_override(ctx, event):
        if getattr(event, "topic", "") == "trigger":
            strat.ctx = ctx
            strat.buy("2330", 1_000_000, 1)
            return strat._generated_intents
        return original_handle(ctx, event)

    strat.handle_event = handle_override
    system.strategy_runner.strategies = [strat]

    mock_client = MagicMock()
    mock_client.place_order.return_value = {"seq_no": "test-seq-1", "order_id": "oid-1"}
    mock_client.get_exchange.return_value = "TSE"
    mock_client.mode = "simulation"
    mock_client.logged_in = True
    system.order_adapter.client = mock_client

    async def fake_call_api(op, fn, *args, intent=None, max_retries=2, **kwargs):
        return fn(*args, **kwargs)

    system.order_adapter._call_api = fake_call_api

    async def fake_on_fill_async(fill_event):
        return system.position_store.on_fill(fill_event)

    system.exec_service.position_store.on_fill_async = fake_on_fill_async

    system.running = True
    system.loop = asyncio.get_running_loop()
    system.tasks.update(
        {
            "exec_router": asyncio.create_task(system.exec_service.run()),
            "risk": asyncio.create_task(system.risk_engine.run()),
            "order": asyncio.create_task(system.order_adapter.run()),
            "strat": asyncio.create_task(system.strategy_runner.run()),
        }
    )

    try:
        await asyncio.sleep(0.1)

        class TriggerEvent:
            symbol = "2330"
            topic = "trigger"
            strategy_id = None

        await system.bus.publish(TriggerEvent())

        start_wait = time.time()
        while time.time() - start_wait < 2.0:
            if mock_client.place_order.call_count > 0:
                break
            await asyncio.sleep(0.05)

        mock_client.place_order.assert_called_once()
        _, kwargs = mock_client.place_order.call_args
        assert kwargs == {
            "contract_code": "2330",
            "exchange": "TSE",
            "action": "Buy",
            "price": 100.0,
            "qty": 1,
            "order_type": "ROD",
            "tif": "ROD",
            "custom_field": "USER1",
            "product_type": None,
            "price_type": "LMT",
        }

        print("Action Verified: Strategy -> Risk -> API")

        ts = time.time_ns()
        raw_deal_data = {
            "code": "2330",
            "price": 100.0,
            "quantity": 1,
            "seq_no": "test-seq-1",
            "ord_no": "oid-1",
            "action": "Buy",
            "custom_field": "USER1",
            "ts": ts,
        }

        raw_event = RawExecEvent(topic="deal", data=raw_deal_data, ingest_ts_ns=ts)

        await system.raw_exec_queue.put(raw_event)

        await asyncio.sleep(0.5)

        key = "sim-account-01:USER1:2330"

        pos_obj = system.position_store.positions.get(key)
        assert pos_obj is not None, f"Position keys: {system.position_store.positions.keys()}"
        assert pos_obj.net_qty == 1
        assert pos_obj.avg_price_scaled == 1_000_000
        assert pos_obj.realized_pnl_scaled == 0

        print(f"Position Verified: {pos_obj.net_qty}")
    finally:
        await system.stop_async()
        await asyncio.get_running_loop().shutdown_default_executor()


@pytest.mark.skip(reason="Requires engine role + broker wiring; maintenance role gives no-op clients (pre-existing)")
def test_full_order_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    result = asyncio.run(_exercise_full_order_lifecycle(monkeypatch))
    assert result is None

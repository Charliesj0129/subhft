import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import OrderStatus, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent
from hft_platform.events import MetaData, TickEvent
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import Position, PositionStore
from hft_platform.order.adapter import OrderAdapter
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner


@pytest.mark.regression
@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_broker_id_cleanup_and_strategy_resolution(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    client.get_exchange.return_value = "TSE"
    client.place_order.return_value = {"seq_no": "S1", "ord_no": "O1"}

    adapter = OrderAdapter("config/dummy.yaml", queue, client)

    intent = OrderIntent(
        intent_id=1,
        strategy_id="stratX",
        symbol="2330",
        side=Side.BUY,
        price=10000,
        qty=1,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )
    cmd = OrderCommand(1, intent, time.time_ns() + 1_000_000_000, 0)
    asyncio.run(adapter._dispatch_to_api(cmd))

    assert adapter.order_id_map["S1"] == "stratX:1"
    assert adapter.order_id_map["O1"] == "stratX:1"

    asyncio.run(adapter.on_terminal_state("stratX", "O1"))
    assert "stratX:1" not in adapter.live_orders

    norm = ExecutionNormalizer(order_id_map={"O1": "stratX:1"})
    raw = RawExecEvent(
        "order",
        {
            "ord_no": "O1",
            "status": {"status": "Submitted"},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 500, "quantity": 1},
        },
        time.time_ns(),
    )
    event = norm.normalize_order(raw)
    assert event.status == OrderStatus.SUBMITTED
    assert event.strategy_id == "stratX"


@pytest.mark.regression
def test_strategy_positions_view():
    class CaptureStrategy(BaseStrategy):
        def __init__(self, strategy_id: str):
            super().__init__(strategy_id=strategy_id)
            self.last_position = None

        def on_tick(self, event):
            self.last_position = self.position(event.symbol)

    runner = StrategyRunner(bus=MagicMock(), risk_queue=asyncio.Queue(), lob_engine=None, config_path="dummy")
    runner.strategies = []
    runner._strat_executors = []

    store = PositionStore()
    store.positions = {
        "acc:alpha:2330": Position("acc", "alpha", "2330", net_qty=7),
        "acc:beta:2330": Position("acc", "beta", "2330", net_qty=2),
    }
    runner.position_store = store

    strat = CaptureStrategy("alpha")
    runner.register(strat)

    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=0, local_ts=0),
        symbol="2330",
        price=0,
        volume=0,
        total_volume=0,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )
    asyncio.run(runner.process_event(event))

    assert strat.last_position == 7


@pytest.mark.regression
def test_execution_normalizer_price_scale(monkeypatch, tmp_path):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'SYM'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    norm = ExecutionNormalizer()
    raw = RawExecEvent(
        "deal",
        {
            "seq_no": "D1",
            "ord_no": "O1",
            "code": "SYM",
            "action": "Buy",
            "quantity": 1,
            "price": 1.23,
            "ts": time.time_ns(),
        },
        time.time_ns(),
    )

    fill = norm.normalize_fill(raw)
    assert fill.price == 123


@pytest.mark.regression
@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_long_strategy_id_fallback(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    client.get_exchange.return_value = "TSE"
    client.place_order.return_value = {"seq_no": "S99", "ord_no": "O99"}

    adapter = OrderAdapter("config/dummy.yaml", queue, client)

    strategy_id = "strategy_long_id"
    intent = OrderIntent(
        intent_id=9,
        strategy_id=strategy_id,
        symbol="2330",
        side=Side.BUY,
        price=10000,
        qty=1,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )
    cmd = OrderCommand(1, intent, time.time_ns() + 1_000_000_000, 0)
    asyncio.run(adapter._dispatch_to_api(cmd))

    assert client.place_order.call_args.kwargs["custom_field"] == ""
    assert adapter.order_id_map["S99"] == f"{strategy_id}:9"
    assert adapter.order_id_map["O99"] == f"{strategy_id}:9"

    norm = ExecutionNormalizer(order_id_map=adapter.order_id_map)
    raw = RawExecEvent(
        "order",
        {
            "ord_no": "O99",
            "status": {"status": "Submitted"},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 500, "quantity": 1},
        },
        time.time_ns(),
    )
    event = norm.normalize_order(raw)
    assert event.strategy_id == strategy_id


@pytest.mark.regression
@pytest.mark.parametrize(
    ("order_id", "mapping", "expected"),
    [
        ("O1", {"strategy_id": "S1", "intent_id": 11}, "S1"),
        ("O2", ("S2", 22), "S2"),
        ("O3", ["S3", 33], "S3"),
        ("O4", "S4:44", "S4"),
        ("O5", "S5", "S5"),
    ],
)
def test_order_id_map_shapes(order_id, mapping, expected):
    norm = ExecutionNormalizer(order_id_map={order_id: mapping})
    raw = RawExecEvent(
        "order",
        {
            "ord_no": order_id,
            "status": {"status": "Submitted"},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 500, "quantity": 1},
        },
        time.time_ns(),
    )
    event = norm.normalize_order(raw)
    assert event.strategy_id == expected

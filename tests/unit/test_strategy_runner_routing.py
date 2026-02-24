import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import OrderEvent, OrderStatus, Side
from hft_platform.contracts.strategy import TIF
from hft_platform.events import MetaData, TickEvent
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner


class OrderStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, symbols=None):
        super().__init__(strategy_id=strategy_id, symbols=symbols or [])
        self.called = 0

    def on_order(self, event: OrderEvent) -> None:
        self.called += 1
        self.buy(event.symbol, 1.0, 1, tif=TIF.LIMIT)

    def on_tick(self, event: TickEvent) -> None:
        self.called += 1
        self.buy(event.symbol, 1.0, 1, tif=TIF.LIMIT)


@pytest.mark.asyncio
async def test_targeted_strategy_only_receives_order_event():
    bus = MagicMock()
    risk_queue = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        MockReg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

    alpha = OrderStrategy("alpha", symbols=["AAA"])
    beta = OrderStrategy("beta", symbols=["AAA"])
    runner.register(alpha)
    runner.register(beta)

    event = OrderEvent(
        order_id="O1",
        strategy_id="alpha",
        symbol="ZZZ",
        status=OrderStatus.SUBMITTED,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=1,
        price=10000,
        side=Side.BUY,
        ingest_ts_ns=time.time_ns(),
        broker_ts_ns=time.time_ns(),
    )

    await runner.process_event(event)

    assert alpha.called == 1
    assert beta.called == 0
    assert risk_queue.qsize() == 1


@pytest.mark.asyncio
async def test_broadcast_event_hits_all_strategies():
    bus = MagicMock()
    risk_queue = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        MockReg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

    a = OrderStrategy("a", symbols=["AAA"])
    b = OrderStrategy("b", symbols=["AAA"])
    runner.strategies = [a, b]
    runner._strat_executors = []

    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="AAA",
        price=100,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )

    await runner.process_event(event)

    assert a.called == 1
    assert b.called == 1
    assert risk_queue.qsize() == 2


@pytest.mark.asyncio
async def test_intent_carries_source_ts_and_trace_id():
    bus = MagicMock()
    risk_queue = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        MockReg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

    strat = OrderStrategy("alpha", symbols=["AAA"])
    runner.register(strat)

    event = TickEvent(
        meta=MetaData(seq=7, topic="tick", source_ts=1, local_ts=123456789),
        symbol="AAA",
        price=100,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )

    await runner.process_event(event)
    intent = await risk_queue.get()

    assert intent.source_ts_ns == 123456789
    assert intent.trace_id == "tick:7"


@pytest.mark.asyncio
async def test_typed_intent_channel_fastpath():
    bus = MagicMock()

    class TypedQueue:
        def __init__(self):
            self.frames = []

        def submit_nowait(self, intent):
            self.frames.append(("legacy", intent))

        def submit_typed_nowait(self, frame):
            self.frames.append(("typed", frame))
            return "ok"

        def qsize(self):
            return len(self.frames)

    risk_queue = TypedQueue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        MockReg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

    strat = OrderStrategy("alpha", symbols=["AAA"])
    runner.register(strat)

    event = TickEvent(
        meta=MetaData(seq=11, topic="tick", source_ts=1, local_ts=99),
        symbol="AAA",
        price=100,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )

    await runner.process_event(event)
    assert risk_queue.qsize() == 1
    mode, payload = risk_queue.frames[0]
    assert mode == "typed"
    assert payload[0] == "typed_intent_v1"
    assert payload[2] == "alpha"
    assert payload[13] == "tick:11"


def test_strategy_runner_obs_policy_minimal_defaults(monkeypatch):
    monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
    bus = MagicMock()
    risk_queue = asyncio.Queue()
    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        MockReg.return_value.instantiate.return_value = []
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")
    assert runner._diagnostic_metrics_enabled is False
    assert runner._strategy_metrics_sample_every >= 8
    assert runner._strategy_metrics_batch >= 32

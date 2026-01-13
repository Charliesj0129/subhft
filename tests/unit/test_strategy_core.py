import asyncio
from unittest.mock import MagicMock, Mock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import Side
from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.strategy.registry import StrategyRegistry
from hft_platform.strategy.runner import StrategyRunner


class DummyStrategy(BaseStrategy):
    def on_tick(self, event):
        self.buy(event.symbol, 100, 1)

def test_strategy_context_place_order():
    intent_factory = Mock(return_value="intent")
    scaler = Mock(return_value=100)
    ctx = StrategyContext(
        positions={"AAPL": 10},
        strategy_id="test_strat",
        intent_factory=intent_factory,
        price_scaler=scaler
    )

    res = ctx.place_order(symbol="AAPL", side=Side.BUY, price=1.0, qty=1)
    assert res == "intent"
    scaler.assert_called_with("AAPL", 1.0)
    intent_factory.assert_called_once()
    assert intent_factory.call_args[1]['price'] == 100

def test_base_strategy_dispatch_and_helpers():
    strat = DummyStrategy(strategy_id="test_strat", symbols=["AAPL"])
    ctx = Mock(spec=StrategyContext)
    ctx.place_order.return_value = "order_intent"
    ctx.positions = {"AAPL": 50}

    # Tick Event
    event = TickEvent(
        symbol="AAPL",
        price=100,
        volume=10,
        total_volume=1000,
        bid_side_total_vol=500,
        ask_side_total_vol=500,
        is_simtrade=False,
        is_odd_lot=False,
        meta=MagicMock()
    )
    intents = strat.handle_event(ctx, event)
    assert len(intents) == 1
    assert intents[0] == "order_intent"

    # Test helpers via the dummy strat call
    # strat.buy was called in on_tick
    strat.sell("AAPL", 101, 1)
    strat.cancel("AAPL", "order_1")

    assert len(strat._generated_intents) == 3 # 1 from on_tick + sell + cancel

    # Position helper
    assert strat.position("AAPL") == 50
    assert strat.position("MSFT") == 0

def test_base_strategy_other_events():
    strat = DummyStrategy(strategy_id="test_strat", symbols=["AAPL"])
    ctx = Mock(spec=StrategyContext)
    strat.ctx = ctx

    meta = MagicMock()

    # Fill
    fill = FillEvent(
        fill_id="f1", account_id="acc1", order_id="o1", strategy_id="test_strat",
        symbol="AAPL", side=Side.BUY, qty=1, price=100, fee=0, tax=0,
        ingest_ts_ns=0, match_ts_ns=0
    )
    strat.handle_event(ctx, fill)

    # Order
    order = OrderEvent(
        order_id="o1", strategy_id="test_strat", symbol="AAPL", status=OrderStatus.FILLED,
        submitted_qty=1, filled_qty=1, remaining_qty=0, price=100, side=Side.BUY,
        ingest_ts_ns=0, broker_ts_ns=0
    )
    strat.handle_event(ctx, order)

    # LOBStats
    stats = LOBStatsEvent(
        symbol="AAPL", ts=0, mid_price=100.5, spread=1.0, imbalance=0.1,
        best_bid=100, best_ask=101, bid_depth=10, ask_depth=10
    )
    strat.handle_event(ctx, stats)

    # BidAsk
    ba = BidAskEvent(
        meta=meta, symbol="AAPL", bids=[], asks=[], is_snapshot=False
    )
    strat.handle_event(ctx, ba)

def test_strategy_symbol_filtering():
    strat = DummyStrategy("test", symbols=["AAPL"])
    ctx = Mock(spec=StrategyContext)
    strat.ctx = ctx

    # 1. Market Data for UNKNOWN symbol -> Should be ignored
    tick_bad = TickEvent(
        symbol="MSFT", price=100, volume=10, total_volume=1000,
        bid_side_total_vol=0, ask_side_total_vol=0, is_simtrade=False, is_odd_lot=False, meta=MagicMock()
    )
    res = strat.handle_event(ctx, tick_bad)
    assert len(res) == 0

    # 2. Execution Event for UNKNOWN symbol -> Should be PROCESSED
    # Strategy might hold legacy positions in symbols it no longer subscribes to
    fill_unknown = FillEvent(
        fill_id="f2", account_id="acc1", order_id="o2", strategy_id="test",
        symbol="MSFT", side=Side.SELL, qty=1, price=100, fee=0, tax=0,
        ingest_ts_ns=0, match_ts_ns=0
    )
    # This calls on_fill. Dummy doesn't implement it but handle_event returns list.
    # Since on_fill is pass, no intents generated, but we verify it didn't return early.
    # We can spy on on_fill
    with patch.object(strat, 'on_fill') as mock_fill:
        strat.handle_event(ctx, fill_unknown)
        mock_fill.assert_called_once()


def test_registry_load_and_instantiate(tmp_path):
    config_file = tmp_path / "strategies.yaml"
    config_file.write_text("""
strategies:
  - id: "TEST_STRAT"
    module: "hft_platform.strategy.base"
    class: "BaseStrategy"
    enabled: true
    params:
      foo: "bar"
""")

    registry = StrategyRegistry(config_path=str(config_file))
    assert len(registry.configs) == 1
    strats = registry.instantiate()
    assert len(strats) == 1

@pytest.mark.asyncio
async def test_strategy_runner_flow():
    bus = MagicMock()
    # Async iterator mock
    events = [TickEvent(
        symbol="AAPL",
        price=100,
        volume=10,
        total_volume=1000,
        bid_side_total_vol=500,
        ask_side_total_vol=500,
        is_simtrade=False,
        is_odd_lot=False,
        meta=MagicMock()
    )]

    async def event_gen():
        for e in events:
            yield e

    bus.consume.return_value = event_gen()
    risk_queue = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        strat = DummyStrategy("algo1", symbols=["AAPL"])
        MockReg.return_value.instantiate.return_value = [strat]

        runner = StrategyRunner(bus, risk_queue, config_path="dummy")
        runner.symbol_metadata.price_scale = Mock(return_value=1.0)

        await runner.process_event(events[0])

        assert not risk_queue.empty()

def test_runner_error_handling():
    bus = MagicMock()
    risk_queue = asyncio.Queue()

    with patch("hft_platform.strategy.runner.StrategyRegistry") as MockReg:
        strat = MagicMock()
        strat.strategy_id = "fail"
        strat.enabled = True
        strat.handle_event.side_effect = ValueError("Fail")

        MockReg.return_value.instantiate.return_value = [strat]
        runner = StrategyRunner(bus, risk_queue, config_path="dummy")

        # Should not raise
        asyncio.run(runner.process_event(MagicMock()))

def test_helpers_without_context():
    strat = DummyStrategy("test")
    # No ctx set
    strat.buy("A", 100, 1) # Should return early
    assert len(strat._generated_intents) == 0
    strat.sell("A", 100, 1)
    assert len(strat._generated_intents) == 0
    strat.cancel("A", "o1")
    assert len(strat._generated_intents) == 0
    assert strat.position("A") == 0

def test_runner_register_and_run():
    # Test register
    runner = StrategyRunner(bus=MagicMock(), risk_queue=asyncio.Queue(), lob_engine=None, config_path="dummy")
    runner.symbol_metadata.price_scale = Mock(return_value=1.0) # Mock scaling

    strat = DummyStrategy("algo2", symbols=["A"])
    runner.register(strat)
    assert strat in runner.strategies

    # Test run() loop
    bus = MagicMock()
    evt = TickEvent(symbol="A", price=100, volume=10, total_volume=1000,
               bid_side_total_vol=500, ask_side_total_vol=500, is_simtrade=False, is_odd_lot=False, meta=MagicMock())

    async def fast_gen():
        yield evt
        # finish

    bus.consume.return_value = fast_gen()
    runner.bus = bus

    # We rely on configured strategies. Runner creates context for us.
    # We must ensure no exception in process_event

    asyncio.run(runner.run())

    # Verify strat logic executed
    # DummyStrategy on_tick calls buy -> place_order -> intent factory
    # Intent factory increments seq.
    assert runner._intent_seq > 0

def test_runner_internals():
    # Test disabled strategy
    runner = StrategyRunner(MagicMock(), asyncio.Queue(), "dummy")
    strat_disabled = DummyStrategy("disabled")
    strat_disabled.enabled = False
    runner.register(strat_disabled)

    # Process event
    # Mock event
    evt = MagicMock()
    evt.strategy_id = None

    # Should skip dispatch
    with patch.object(strat_disabled, 'handle_event') as mock_handle:
        asyncio.run(runner.process_event(evt))
        mock_handle.assert_not_called()

    # Test scale price directly
    runner.symbol_metadata.price_scale = Mock(return_value=100.0)
    price_int = runner._scale_price("A", 1.5)
    assert price_int == 150


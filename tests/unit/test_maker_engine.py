"""Tests for MakerEngine — CK-direct maker backtest."""

import pytest

from research.backtest.cost_models import TAIFEXCost
from research.backtest.fill_models import QueueDepletionFill
from research.backtest.maker_engine import (
    ClickHouseSource,
    MakerEngine,
    TickData,
)


def test_maker_engine_properties():
    cost = TAIFEXCost("TMFD6", 1.3, 0.7, 10)
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(fill_model=fill, cost_model=cost)
    assert engine.engine_type == "maker"
    assert engine.fill_model_name == "QueueDepletion(qf=0.5)"


def test_ck_health_check_raises_on_failure():
    source = ClickHouseSource(host="invalid-host-xyz", port=1)
    with pytest.raises(ConnectionError, match="ClickHouse"):
        source.health_check()


def test_tick_data_spread_pts():
    tick = TickData(
        exch_ts=1000,
        bid_price=100_000_000,
        ask_price=104_000_000,
        bid_qty=50,
        ask_qty=30,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=1_000_000,
    )
    assert tick.spread_pts == 4

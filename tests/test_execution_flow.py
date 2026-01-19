import asyncio
import time

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore


@pytest.fixture
def symbols_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))
    return cfg


@pytest.mark.asyncio
async def test_normalizer_flow(symbols_cfg):
    q = asyncio.Queue()
    norm = ExecutionNormalizer(q)

    # Test Order Normalization
    ts = time.time_ns()
    raw_order = RawExecEvent(
        "order",
        {
            "ord_no": "O123",
            "status": {"status": "Submitted"},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 500, "quantity": 1},
        },
        ts,
    )

    event = norm.normalize_order(raw_order)
    assert isinstance(event, OrderEvent)
    assert event.order_id == "O123"
    assert event.status == OrderStatus.SUBMITTED
    assert event.symbol == "2330"
    assert event.price == 5000000
    assert event.side == Side.BUY

    # Test Fill Normalization
    raw_fill = RawExecEvent(
        "deal",
        {"seq_no": "D001", "ord_no": "O123", "code": "2330", "action": "Buy", "quantity": 1, "price": 500, "ts": ts},
        ts,
    )

    fill = norm.normalize_fill(raw_fill)
    assert isinstance(fill, FillEvent)
    assert fill.fill_id == "D001"
    assert fill.price == 5000000


def test_normalizer_strategy_id_from_order_key(symbols_cfg):
    norm = ExecutionNormalizer(order_id_map={"O123": "stratA:7"})

    raw_order = RawExecEvent(
        "order",
        {
            "ord_no": "O123",
            "status": {"status": "Submitted"},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 500, "quantity": 1},
        },
        time.time_ns(),
    )

    event = norm.normalize_order(raw_order)
    assert isinstance(event, OrderEvent)
    assert event.strategy_id == "stratA"


@pytest.mark.asyncio
async def test_position_tracking():
    store = PositionStore()

    # Buy 1 @ 500
    fill1 = FillEvent("D1", "ACC1", "O1", "S1", "2330", Side.BUY, 1, 5000000, 0, 0, 0, 0)
    delta1 = store.on_fill(fill1)

    assert delta1.net_qty == 1
    assert delta1.avg_price == 5000000
    assert delta1.realized_pnl == 0

    # Buy 1 @ 600
    fill2 = FillEvent("D2", "ACC1", "O2", "S1", "2330", Side.BUY, 1, 6000000, 0, 0, 0, 0)
    delta2 = store.on_fill(fill2)

    assert delta2.net_qty == 2
    assert delta2.avg_price == 5500000  # (500+600)/2

    # Sell 2 @ 700 (Close all)
    fill3 = FillEvent("D3", "ACC1", "O3", "S1", "2330", Side.SELL, 2, 7000000, 0, 0, 0, 0)
    delta3 = store.on_fill(fill3)

    assert delta3.net_qty == 0
    # PnL: 2 * (700 - 550) = 300
    assert delta3.realized_pnl == 3000000

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter


class _Meta:
    def __init__(self, scale: int):
        self._scale = scale

    def price_scale(self, symbol: str) -> int:
        return self._scale


def _intent(intent_type, **overrides):
    base = dict(
        intent_id=1,
        strategy_id="strat",
        symbol="AAA",
        side=Side.BUY,
        price=10000,
        qty=1,
        intent_type=intent_type,
        tif=TIF.LIMIT,
        target_order_id=None,
    )
    base.update(overrides)
    return OrderIntent(**base)


def _cmd(intent):
    return OrderCommand(cmd_id=1, intent=intent, deadline_ns=time.time_ns() + 1_000_000_000, storm_guard_state=0)


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_cancel_and_amend_paths(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    adapter = OrderAdapter("config/dummy.yaml", queue, client)
    adapter.metadata = _Meta(scale=100)

    trade = {"id": "T1"}
    adapter.live_orders["strat:10"] = trade

    cancel_intent = _intent(IntentType.CANCEL, target_order_id=10)
    asyncio.run(adapter.execute(_cmd(cancel_intent)))
    client.cancel_order.assert_called_with(trade)

    amend_intent = _intent(IntentType.AMEND, target_order_id=10, price=12300)
    asyncio.run(adapter.execute(_cmd(amend_intent)))
    client.update_order.assert_called_with(trade, price=123.0)


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_cancel_and_amend_use_broker_id_map(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    adapter = OrderAdapter("config/dummy.yaml", queue, client)
    adapter.metadata = _Meta(scale=100)

    trade = {"id": "T1"}
    adapter.live_orders["strat:10"] = trade
    adapter.order_id_map["O10"] = "strat:10"

    cancel_intent = _intent(IntentType.CANCEL, target_order_id="O10")
    asyncio.run(adapter.execute(_cmd(cancel_intent)))
    client.cancel_order.assert_called_with(trade)

    amend_intent = _intent(IntentType.AMEND, target_order_id="O10", price=12300)
    asyncio.run(adapter.execute(_cmd(amend_intent)))
    client.update_order.assert_called_with(trade, price=123.0)


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_rate_limit_hard_stops(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    adapter = OrderAdapter("config/dummy.yaml", queue, client)
    adapter.rate_limiter.update(hard_cap=2, window_s=10)

    now = time.time()
    adapter.rate_limiter.rate_window.append(now)
    adapter.rate_limiter.rate_window.append(now)

    assert adapter.check_rate_limit() is False


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_circuit_breaker_blocks(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    client.get_exchange.return_value = "TSE"
    client.place_order.side_effect = RuntimeError("boom")

    adapter = OrderAdapter("config/dummy.yaml", queue, client)
    adapter.circuit_breaker.threshold = 2
    adapter.circuit_breaker.timeout_s = 60
    adapter.metadata = _Meta(scale=100)

    intent = _intent(IntentType.NEW)
    asyncio.run(adapter.execute(_cmd(intent)))
    asyncio.run(adapter.execute(_cmd(intent)))

    assert adapter.circuit_breaker.open_until > time.time()

    asyncio.run(adapter.execute(_cmd(intent)))
    assert client.place_order.call_count == 2


@pytest.mark.parametrize(
    "mapping",
    [
        {"strategy_id": "strat", "intent_id": 11},
        ("strat", 11),
        ["strat", 11],
        "strat:11",
    ],
)
@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_terminal_cleanup_resolves_mapping_shapes(mock_load, mapping):
    queue = asyncio.Queue()
    client = MagicMock()
    adapter = OrderAdapter("config/dummy.yaml", queue, client)

    adapter.live_orders["strat:11"] = {"id": "T11"}
    adapter.order_id_map["O11"] = mapping

    adapter.on_terminal_state("strat", "O11")
    assert "strat:11" not in adapter.live_orders

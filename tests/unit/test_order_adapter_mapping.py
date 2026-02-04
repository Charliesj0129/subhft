import asyncio
import time
from unittest.mock import MagicMock, patch

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
def test_order_adapter_maps_broker_ids_and_clears(mock_load):
    queue = asyncio.Queue()
    client = MagicMock()
    client.get_exchange.return_value = "TSE"
    client.place_order.return_value = {"seq_no": "S1", "ord_no": "O1"}

    adapter = OrderAdapter("config/dummy.yaml", queue, client)

    intent = OrderIntent(
        intent_id=1,
        strategy_id="strat1",
        symbol="2330",
        side=Side.BUY,
        price=10000,
        qty=1,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )
    cmd = OrderCommand(1, intent, time.time_ns() + 1_000_000_000, 0)
    asyncio.run(adapter._dispatch_to_api(cmd))

    order_key = "strat1:1"
    assert adapter.order_id_map["S1"] == order_key
    assert adapter.order_id_map["O1"] == order_key

    adapter.on_terminal_state("strat1", "O1")
    assert order_key not in adapter.live_orders

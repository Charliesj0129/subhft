from enum import Enum

from hft_platform.contracts.execution import OrderEvent, OrderStatus
from hft_platform.contracts.strategy import Side
from hft_platform.utils.serialization import serialize


class Color(Enum):
    RED = "red"


class WithDict:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return {"value": self.value}


def test_serialize_slots_and_enums():
    event = OrderEvent(
        order_id="O1",
        strategy_id="S1",
        symbol="AAA",
        status=OrderStatus.SUBMITTED,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=1,
        price=10000,
        side=Side.BUY,
        ingest_ts_ns=1,
        broker_ts_ns=2,
    )

    payload = serialize(event)
    assert payload["order_id"] == "O1"
    assert payload["status"] == OrderStatus.SUBMITTED.value
    assert payload["side"] == Side.BUY.value


def test_serialize_nested_structures():
    data = {
        "color": Color.RED,
        "list": [WithDict(1), {"inner": WithDict(2)}],
    }
    payload = serialize(data)
    assert payload["color"] == "red"
    assert payload["list"][0]["value"] == 1
    assert payload["list"][1]["inner"]["value"] == 2

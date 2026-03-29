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


class PlainObject:
    def __init__(self, value):
        self.value = value


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


def test_serialize_dict_fallback_and_scalar():
    payload = serialize(PlainObject(3))
    assert payload["value"] == 3
    assert serialize(42) == 42


class _SlottedMissing:
    """Object with __slots__ where not all slots are set in __init__."""

    __slots__ = ("present", "missing")

    def __init__(self, value: int) -> None:
        self.present = value
        # 'missing' slot intentionally not set


def test_serialize_slots_skips_unset_slot():
    """When a slot exists in __slots__ but the attribute was never set,
    hasattr(obj, k) returns False and that key is omitted from the result."""
    obj = _SlottedMissing(42)
    payload = serialize(obj)
    assert payload == {"present": 42}
    assert "missing" not in payload

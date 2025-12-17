
from dataclasses import dataclass, field
from enum import Enum, auto
from hft_platform.utils.serialization import serialize

class Side(Enum):
    BUY = 1
    SELL = 2

@dataclass(slots=True)
class SlotEvent:
    id: str
    price: int
    side: Side
    
@dataclass
class DictEvent:
    name: str
    val: float

def test_serialization():
    print("Testing Dict...")
    d = {"a": 1, "b": Side.BUY}
    assert serialize(d) == {"a": 1, "b": 1}
    
    print("Testing Slots...")
    s = SlotEvent("o1", 10000, Side.SELL)
    serialized = serialize(s)
    print(f"Slots -> {serialized}")
    assert serialized == {"id": "o1", "price": 10000, "side": 2}
    
    print("Testing Dict Obj...")
    o = DictEvent("test", 1.5)
    ser_o = serialize(o)
    print(f"Obj -> {ser_o}")
    assert ser_o == {"name": "test", "val": 1.5}

    print("PASS: Serialization Logic Verified")

if __name__ == "__main__":
    test_serialization()

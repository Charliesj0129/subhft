import pytest
from hft_platform.rust_core import EventBus

def test_event_bus():
    bus = EventBus()
    bus.push("test_event")
    assert bus.pop() == "test_event"
    assert bus.pop() is None

def test_imports():
    from hft_platform.engine.core import HFTEngine
    from hft_platform.strategies.base import Strategy
    assert True

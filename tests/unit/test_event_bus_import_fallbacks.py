import importlib.util
import sys
from types import ModuleType

import pytest

from hft_platform.core import timebase
from hft_platform.engine import event_bus
from hft_platform.feed_adapter import lob_engine, normalizer


@pytest.mark.parametrize("bus_mode", ["rust_typed", "rust_pyobj"])
def test_removed_backends_ignore_legacy_flags_and_installed_extension(monkeypatch, bus_mode):
    accessed_exports = []

    def record_access(name):
        accessed_exports.append(name)
        raise AttributeError(name)

    stub = ModuleType("hft_platform.rust_core")
    stub.__getattr__ = record_access
    monkeypatch.setitem(sys.modules, "hft_platform.rust_core", stub)
    monkeypatch.setitem(sys.modules, "rust_core", stub)
    monkeypatch.setenv("HFT_RUST_ACCEL", "1")
    monkeypatch.setenv("HFT_BUS_RUST", "1")
    monkeypatch.setenv("HFT_BUS_MODE", bus_mode)
    monkeypatch.setenv("HFT_BUS_TYPED_TICK_RING", "1")
    monkeypatch.setenv("HFT_BUS_TYPED_BOOK_RINGS", "1")
    monkeypatch.setenv("HFT_LOB_RUST_BOOKSTATE", "1")
    monkeypatch.setenv("HFT_FUSED_NORMALIZER", "1")
    monkeypatch.setenv("HFT_TIMEBASE_RUST_COERCE", "1")

    fresh_modules = {}
    for module in (timebase, event_bus, lob_engine, normalizer):
        spec = importlib.util.spec_from_file_location(f"test_isolated_{module.__name__}", module.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        fresh_modules[module.__name__] = fresh

    fresh_bus = fresh_modules[event_bus.__name__]
    bus = fresh_bus.RingBufferBus(size=4)
    bus.publish_nowait("control")
    assert bus.buffer[0] == "control"
    assert isinstance(bus._tick_ring, fresh_bus._PyFastTickRingBuffer)
    assert isinstance(bus._bidask_ring, fresh_bus._PyFastBidAskRingBuffer)
    assert fresh_modules[timebase.__name__].coerce_ns(1_700_000_000) == 1_700_000_000_000_000_000
    assert accessed_exports == []

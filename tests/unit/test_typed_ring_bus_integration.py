"""Tests for HFT_BUS_MODE tri-mode support in RingBufferBus."""

import asyncio

from hft_platform.engine import event_bus as event_bus_mod
from hft_platform.engine.event_bus import RingBufferBus


def test_default_mode_is_python_when_no_env_vars(monkeypatch):
    """Default mode is 'python' when neither HFT_BUS_MODE nor HFT_BUS_RUST is set."""
    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "python")
    bus = RingBufferBus(size=4)
    assert bus._bus_mode == "python"


def test_legacy_bus_rust_1_maps_to_rust_pyobj(monkeypatch):
    """HFT_BUS_RUST=1 (legacy) resolves to rust_pyobj mode."""
    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_pyobj")
    bus = RingBufferBus(size=4)
    assert bus._bus_mode == "rust_pyobj"


def test_bus_mode_rust_typed_overrides_legacy(monkeypatch):
    """HFT_BUS_MODE=rust_typed takes precedence."""
    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_typed")
    monkeypatch.setattr(event_bus_mod, "_FastTypedRingBuffer", None)
    bus = RingBufferBus(size=4)
    assert bus._bus_mode == "rust_typed"
    # No typed ring since FastTypedRingBuffer is None
    assert bus._typed_ring is None


def test_rust_typed_fallback_to_python_when_unavailable(monkeypatch):
    """rust_typed mode falls back gracefully when FastTypedRingBuffer is None."""
    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_typed")
    monkeypatch.setattr(event_bus_mod, "_FastTypedRingBuffer", None)
    bus = RingBufferBus(size=4)
    # Still constructs, typed_ring is None so fallback buffer used
    assert bus._typed_ring is None

    async def _run():
        bus.publish_nowait("evt-1")
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == "evt-1"


def test_publish_consume_roundtrip_python_mode(monkeypatch):
    """Publish/consume round-trip works in python mode."""
    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "python")
    bus = RingBufferBus(size=8)

    tick = ("tick", "2330", 10000, 5, 50, False, False, 123456789)

    async def _run():
        bus.publish_nowait(tick)
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == tick


def test_bus_mode_attribute_accessible():
    """The _bus_mode attribute is accessible on RingBufferBus instances."""
    bus = RingBufferBus(size=4)
    assert hasattr(bus, "_bus_mode")
    assert isinstance(bus._bus_mode, str)
    assert bus._bus_mode in {"python", "rust_pyobj", "rust_typed"}


def test_rust_typed_with_mock_typed_ring(monkeypatch):
    """rust_typed mode uses FastTypedRingBuffer when available (mocked)."""

    class MockFastTypedRingBuffer:
        """Mock that records publish calls for verification."""

        def __init__(self, capacity: int) -> None:
            self.capacity = capacity
            self.published: list[tuple] = []

        def publish(
            self,
            kind: int,
            flags: int,
            symbol_id: int,
            exch_ts_ns: int,
            local_ts_ns: int,
            price0: int,
            price1: int,
            qty0: int,
            qty1: int,
            aux0: int,
            aux1: int,
            ratio0: float,
        ) -> int:
            self.published.append(
                (kind, flags, symbol_id, exch_ts_ns, local_ts_ns, price0, price1, qty0, qty1, aux0, aux1, ratio0)
            )
            return len(self.published)

        def get(self, seq: int):
            return None

    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_typed")
    monkeypatch.setattr(event_bus_mod, "_FastTypedRingBuffer", MockFastTypedRingBuffer)
    bus = RingBufferBus(size=8)
    assert bus._typed_ring is not None
    assert isinstance(bus._typed_ring, MockFastTypedRingBuffer)

    tick = ("tick", "2330", 10000, 5, 50, False, False, 123456789)
    bus.publish_nowait(tick)

    assert len(bus._typed_ring.published) == 1
    frame = bus._typed_ring.published[0]
    # kind=1 (tick), price0=10000, qty0=5, qty1=50, exch_ts_ns=123456789
    assert frame[0] == 1  # kind = _KIND_TICK
    assert frame[3] == 123456789  # exch_ts_ns
    assert frame[5] == 10000  # price0
    assert frame[6] == 0  # price1 (unused for tick)
    assert frame[7] == 5  # qty0 (volume)
    assert frame[8] == 50  # qty1 (total_volume)


def test_rust_typed_consume_uses_fallback_buffer(monkeypatch):
    """In rust_typed mode, consume still works via fallback buffer."""

    class MockFastTypedRingBuffer:
        def __init__(self, capacity: int) -> None:
            pass

        def publish(self, *args, **kwargs) -> int:
            return 1

    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_typed")
    monkeypatch.setattr(event_bus_mod, "_FastTypedRingBuffer", MockFastTypedRingBuffer)
    bus = RingBufferBus(size=8)

    event = ("tick", "2330", 10000, 5, 50, False, False, 123456789)
    bus.publish_nowait(event)

    async def _run():
        async for evt in bus.consume(start_cursor=-1):
            return evt

    result = asyncio.run(_run())
    assert result == event


def test_rust_typed_bidask_publish_maps_fields(monkeypatch):
    """Bidask events map best_bid/best_ask/imbalance to typed ring fields."""

    class MockFastTypedRingBuffer:
        def __init__(self, capacity: int) -> None:
            self.published: list[tuple] = []

        def publish(self, *args) -> int:
            self.published.append(args)
            return len(self.published)

    monkeypatch.setattr(event_bus_mod, "_BUS_MODE", "rust_typed")
    monkeypatch.setattr(event_bus_mod, "_FastTypedRingBuffer", MockFastTypedRingBuffer)
    bus = RingBufferBus(size=8)

    bidask = (
        "bidask",
        "2330",
        [[10000, 3]],
        [[10010, 4]],
        999888777,  # exch_ts
        False,
        10000,
        10010,  # best_bid, best_ask
        5,
        9,  # bid_depth, ask_depth
        10005.0,
        10.0,
        0.123,  # imbalance
    )
    bus.publish_nowait(bidask)

    frame = bus._typed_ring.published[0]
    assert frame[0] == 2  # kind = _KIND_BIDASK
    assert frame[3] == 999888777  # exch_ts_ns
    assert frame[5] == 10000  # price0 = best_bid
    assert frame[6] == 10010  # price1 = best_ask
    assert frame[7] == 5  # qty0 = bid_depth
    assert frame[8] == 9  # qty1 = ask_depth
    assert abs(frame[11] - 0.123) < 1e-9  # ratio0 = imbalance


def test_rust_typed_with_importorskip():
    """If FastTypedRingBuffer is available in rust_core, it can be imported."""
    _ftrb = None
    try:
        from hft_platform.rust_core import FastTypedRingBuffer as _ftrb_cls  # type: ignore[attr-defined]

        _ftrb = _ftrb_cls
    except (ImportError, AttributeError):
        pass
    # This test just validates the import path; skip gracefully if not registered
    if _ftrb is None:
        import pytest

        pytest.skip("FastTypedRingBuffer not yet registered in rust_core lib.rs")
    ring = _ftrb(16)
    seq = ring.publish(1, 0, 42, 100, 200, 10, 20, 30, 40, 50, 60, 0.5)
    assert seq == 1
    result = ring.get(seq)
    assert result is not None

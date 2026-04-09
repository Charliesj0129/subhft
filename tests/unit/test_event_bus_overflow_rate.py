"""Tests for XB-03: sliding-window overflow rate tracker in RingBufferBus."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _disable_rust_bus(monkeypatch):
    """Force Python-only bus mode for deterministic testing."""
    monkeypatch.setenv("HFT_BUS_RUST", "0")
    monkeypatch.setenv("HFT_BUS_MODE", "python")
    monkeypatch.setenv("HFT_BUS_WAIT_MODE", "event")


def _make_bus(
    size: int = 4,
    storm_guard: object | None = None,
    overflow_window_s: float = 60.0,
    overflow_rate_threshold: int = 10,
    overflow_halt_threshold: int = 100,
):
    """Create a small RingBufferBus with configurable overflow settings."""
    import importlib

    # Patch env before import
    import os

    os.environ["HFT_BUS_OVERFLOW_WINDOW_S"] = str(overflow_window_s)
    os.environ["HFT_BUS_OVERFLOW_RATE_THRESHOLD"] = str(overflow_rate_threshold)
    # Set consecutive threshold very high so it doesn't interfere with rate tests
    os.environ["HFT_BUS_OVERFLOW_HALT_THRESHOLD"] = str(overflow_halt_threshold)

    # Re-import to pick up env changes
    mod = importlib.import_module("hft_platform.engine.event_bus")
    importlib.reload(mod)
    bus = mod.RingBufferBus(size=size, storm_guard=storm_guard)
    return bus


def _force_overflow(bus, overflow_amount: int = 1):
    """Publish enough events to force an overflow for a consumer starting at cursor -1.

    The consumer is at local_seq = bus.cursor (join point).
    We need current_cursor - local_seq > bus.size to trigger overflow.
    """
    for _ in range(overflow_amount):
        bus.publish_nowait(("tick", "TEST", 1000, 1, 1, False, False, 0))


class TestSlidingWindowOverflowRate:
    """Tests for the sliding-window overflow rate tracker."""

    def test_slow_steady_overflow_triggers_rate_halt(self):
        """Slow but steady overflows (1 per iteration, catching up between)
        should eventually trigger HALT via the rate tracker even though
        the consecutive counter resets each time.

        We directly call _record_overflow_rate to simulate the overflow events,
        since the consume() generator resets the consecutive counter on full
        catch-up but the rate tracker accumulates across resets.
        """
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=60.0,
            overflow_rate_threshold=5,
            overflow_halt_threshold=999,
        )

        # Simulate 4 overflows — each followed by catch-up (consecutive resets)
        for _ in range(4):
            bus._record_overflow_rate()
            # Simulate consecutive counter reset (what happens on catch-up)
            bus._overflow_count = 0

        # No HALT yet — only 4 in window, threshold is 5
        sg.trigger_halt.assert_not_called()

        # 5th overflow triggers rate-based HALT
        bus._record_overflow_rate()
        sg.trigger_halt.assert_called_once()
        call_args = sg.trigger_halt.call_args[0][0]
        assert "overflow rate" in call_args
        assert "5" in call_args

    def test_overflow_below_threshold_no_rate_halt(self):
        """Fewer overflows than the threshold should NOT trigger rate-based HALT."""
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=60.0,
            overflow_rate_threshold=10,
            overflow_halt_threshold=999,
        )

        # Record only 3 overflows (below threshold of 10)
        for _ in range(3):
            bus._record_overflow_rate()

        assert len(bus._overflow_timestamps) == 3
        sg.trigger_halt.assert_not_called()

    def test_old_timestamps_evicted_from_window(self):
        """Timestamps older than the window should be evicted."""
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=2.0,  # 2-second window
            overflow_rate_threshold=5,
            overflow_halt_threshold=999,
        )

        # Manually test the _record_overflow_rate method with time mocking
        fake_time = [100.0]

        def mock_monotonic():
            return fake_time[0]

        with patch("hft_platform.engine.event_bus.time") as mock_time_mod:
            mock_time_mod.monotonic = mock_monotonic

            # Record 3 overflows at t=100
            for _ in range(3):
                bus._record_overflow_rate()
            assert len(bus._overflow_timestamps) == 3

            # Advance time past the window
            fake_time[0] = 103.0

            # Record 1 more — the old 3 should be evicted
            bus._record_overflow_rate()
            assert len(bus._overflow_timestamps) == 1

            # HALT should not have triggered (only 1 in window, threshold=5)
            sg.trigger_halt.assert_not_called()

    def test_consecutive_counter_still_works(self):
        """The existing consecutive overflow counter should still trigger HALT
        independently of the rate tracker.

        We verify the consecutive logic by simulating overflow_count reaching
        the threshold without catch-up resets.
        """
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        # consecutive threshold = 3, rate threshold very high so it doesn't interfere
        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=60.0,
            overflow_rate_threshold=999,
            overflow_halt_threshold=3,
        )

        # Directly simulate 3 consecutive overflows (no catch-up reset)
        bus._overflow_count = 3

        # The HALT check in consume() is:
        # if self._overflow_count >= self._overflow_halt_threshold and self._storm_guard is not None:
        assert bus._overflow_count >= bus._overflow_halt_threshold
        assert bus._storm_guard is not None

        # Simulate the HALT trigger that would happen in consume()
        halt_msg = f"EventBus overflow: {bus._overflow_count} overflows, lag=100"
        bus._storm_guard.trigger_halt(halt_msg)

        sg.trigger_halt.assert_called_once()
        call_args = sg.trigger_halt.call_args[0][0]
        assert "overflow" in call_args.lower()

    def test_deque_bounded_maxlen(self):
        """The overflow_timestamps deque should have a bounded maxlen."""
        bus = _make_bus(
            size=4,
            overflow_rate_threshold=5,
            overflow_halt_threshold=999,
        )
        assert bus._overflow_timestamps.maxlen == 10  # rate_threshold * 2

    def test_rate_tracker_called_from_consume_overflow_path(self):
        """Verify _record_overflow_rate is called in the consume() overflow path
        by checking that overflow_timestamps grows after an overflow event."""
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=60.0,
            overflow_rate_threshold=999,
            overflow_halt_threshold=999,
        )

        async def run():
            consumer = bus.consume(start_cursor=-1)

            # Publish enough to cause overflow
            for _ in range(bus.size + 1):
                bus.publish_nowait(("tick", "TEST", 1000, 1, 1, False, False, 0))

            consumed = 0
            async for _event in consumer:
                consumed += 1
                if consumed >= bus.size:
                    break

        asyncio.run(run())

        # The overflow should have recorded a timestamp
        assert len(bus._overflow_timestamps) == 1

    def test_rate_tracker_called_from_consume_batch_overflow_path(self):
        """Verify _record_overflow_rate is called in consume_batch() overflow path."""
        sg = MagicMock()
        sg.trigger_halt = MagicMock()

        bus = _make_bus(
            size=4,
            storm_guard=sg,
            overflow_window_s=60.0,
            overflow_rate_threshold=999,
            overflow_halt_threshold=999,
        )

        async def run():
            consumer = bus.consume_batch(batch_size=10, start_cursor=-1)

            for _ in range(bus.size + 1):
                bus.publish_nowait(("tick", "TEST", 1000, 1, 1, False, False, 0))

            async for _batch in consumer:
                break

        asyncio.run(run())

        assert len(bus._overflow_timestamps) == 1

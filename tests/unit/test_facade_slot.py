"""Tests for FacadeState FSM and FacadeSlot data structure.

Verifies foundational per-connection isolation data structures used by
the QuoteConnectionPool per-connection failure isolation refactoring.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState


class TestFacadeStateOrdering:
    """FacadeState IntEnum ordering and membership."""

    def test_connected_is_zero(self) -> None:
        assert FacadeState.CONNECTED == 0

    def test_degraded_is_one(self) -> None:
        assert FacadeState.DEGRADED == 1

    def test_recovering_is_two(self) -> None:
        assert FacadeState.RECOVERING == 2

    def test_disconnected_is_three(self) -> None:
        assert FacadeState.DISCONNECTED == 3

    def test_connected_less_than_degraded(self) -> None:
        assert FacadeState.CONNECTED < FacadeState.DEGRADED

    def test_degraded_less_than_recovering(self) -> None:
        assert FacadeState.DEGRADED < FacadeState.RECOVERING

    def test_recovering_less_than_disconnected(self) -> None:
        assert FacadeState.RECOVERING < FacadeState.DISCONNECTED

    def test_all_states_are_ordered(self) -> None:
        states = [FacadeState.CONNECTED, FacadeState.DEGRADED, FacadeState.RECOVERING, FacadeState.DISCONNECTED]
        assert states == sorted(states)


class TestFacadeStateIsHealthy:
    """FacadeState.is_healthy() only returns True for CONNECTED."""

    def test_connected_is_healthy(self) -> None:
        assert FacadeState.CONNECTED.is_healthy() is True

    def test_degraded_is_not_healthy(self) -> None:
        assert FacadeState.DEGRADED.is_healthy() is False

    def test_recovering_is_not_healthy(self) -> None:
        assert FacadeState.RECOVERING.is_healthy() is False

    def test_disconnected_is_not_healthy(self) -> None:
        assert FacadeState.DISCONNECTED.is_healthy() is False


class TestFacadeSlotDefaults:
    """FacadeSlot initializes with correct default values."""

    def test_state_defaults_to_recovering(self) -> None:
        """Initial state is RECOVERING until subscribe_all completes (H3 fix)."""
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert slot.state is FacadeState.RECOVERING

    def test_reconnect_failures_defaults_to_zero(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert slot.reconnect_failures == 0

    def test_symbols_defaults_to_empty_set(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert slot.symbols == set()

    def test_conn_id_is_stored(self) -> None:
        slot = FacadeSlot(conn_id="conn-42", facade=MagicMock())
        assert slot.conn_id == "conn-42"

    def test_facade_is_stored(self) -> None:
        facade = MagicMock()
        slot = FacadeSlot(conn_id="c0", facade=facade)
        assert slot.facade is facade

    def test_degraded_since_mono_defaults_to_none(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert slot.degraded_since_mono is None

    def test_last_data_mono_is_float(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert isinstance(slot.last_data_mono, float)

    def test_last_reconnect_mono_is_float(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert isinstance(slot.last_reconnect_mono, float)


class TestFacadeSlotFeedGap:
    """FacadeSlot.feed_gap_s() returns elapsed time since last data."""

    def test_feed_gap_is_non_negative(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert slot.feed_gap_s() >= 0.0

    def test_feed_gap_increases_over_time(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        gap1 = slot.feed_gap_s()
        time.sleep(0.01)
        gap2 = slot.feed_gap_s()
        assert gap2 > gap1

    def test_feed_gap_reflects_last_data_mono(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        # Set last_data_mono to 2 seconds ago
        slot.last_data_mono = time.monotonic() - 2.0
        gap = slot.feed_gap_s()
        assert 1.9 <= gap <= 2.5

    def test_feed_gap_after_update(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.last_data_mono = time.monotonic() - 10.0
        old_gap = slot.feed_gap_s()
        slot.last_data_mono = time.monotonic()
        new_gap = slot.feed_gap_s()
        assert new_gap < old_gap


class TestFacadeSlotBackoff:
    """FacadeSlot.backoff_s() computes exponential backoff capped at 120s."""

    def test_backoff_zero_failures(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.reconnect_failures = 0
        # 5.0 * (2**0) = 5.0
        assert slot.backoff_s() == pytest.approx(5.0)

    def test_backoff_one_failure(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.reconnect_failures = 1
        # 5.0 * (2**1) = 10.0
        assert slot.backoff_s() == pytest.approx(10.0)

    def test_backoff_five_failures(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.reconnect_failures = 5
        # 5.0 * (2**5) = 160.0 → capped at 120.0
        assert slot.backoff_s() == pytest.approx(120.0)

    def test_backoff_ten_failures_capped(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.reconnect_failures = 10
        # 5.0 * (2**10) = 5120.0 → capped at 120.0
        assert slot.backoff_s() == pytest.approx(120.0)

    def test_backoff_cap_is_120(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        for failures in range(4, 20):
            slot.reconnect_failures = failures
            assert slot.backoff_s() <= 120.0

    def test_backoff_strictly_increases_before_cap(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.reconnect_failures = 0
        b0 = slot.backoff_s()
        slot.reconnect_failures = 1
        b1 = slot.backoff_s()
        slot.reconnect_failures = 2
        b2 = slot.backoff_s()
        assert b0 < b1 < b2


class TestFacadeSlotSlots:
    """FacadeSlot uses __slots__ for memory efficiency."""

    def test_has_slots_defined(self) -> None:
        assert hasattr(FacadeSlot, "__slots__")

    def test_slots_contains_expected_fields(self) -> None:
        expected = {
            "conn_id",
            "facade",
            "state",
            "symbols",
            "last_data_mono",
            "last_reconnect_mono",
            "reconnect_failures",
            "degraded_since_mono",
        }
        assert expected.issubset(set(FacadeSlot.__slots__))

    def test_no_dict_attribute(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        assert not hasattr(slot, "__dict__")


class TestFacadeSlotThreadSafety:
    """P1 (2026-04-24): compound state transitions under per-slot lock."""

    def test_begin_reconnect_returns_true_first_call(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.state = FacadeState.DEGRADED
        assert slot.begin_reconnect() is True
        assert slot.state is FacadeState.RECOVERING

    def test_begin_reconnect_returns_false_when_already_recovering(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.state = FacadeState.RECOVERING
        assert slot.begin_reconnect() is False

    def test_record_reconnect_success_sets_connected(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.state = FacadeState.RECOVERING
        slot.reconnect_failures = 3
        slot.degraded_since_mono = 123.0
        slot.record_reconnect_success()
        assert slot.state is FacadeState.CONNECTED
        assert slot.reconnect_failures == 0
        assert slot.degraded_since_mono is None
        assert slot._pending_warmup_reset is True

    def test_record_reconnect_failure_increments_and_disconnects(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.state = FacadeState.RECOVERING
        slot.reconnect_failures = 2
        slot.record_reconnect_failure()
        assert slot.state is FacadeState.DISCONNECTED
        assert slot.reconnect_failures == 3

    def test_snapshot_returns_consistent_triple(self) -> None:
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        slot.state = FacadeState.DEGRADED
        slot.reconnect_failures = 7
        slot.degraded_since_mono = 42.0
        state, failures, since = slot.snapshot()
        assert state is FacadeState.DEGRADED
        assert failures == 7
        assert since == 42.0

    def test_concurrent_record_reconnect_failure_no_lost_updates(self) -> None:
        """Daemon reconnect threads may race — reconnect_failures must not lose
        increments (P1 regression test)."""
        slot = FacadeSlot(conn_id="c0", facade=MagicMock())
        threads_count = 8
        per_thread = 500
        expected = threads_count * per_thread

        def bump() -> None:
            for _ in range(per_thread):
                slot.record_reconnect_failure()

        threads = [threading.Thread(target=bump, daemon=True) for _ in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert slot.reconnect_failures == expected, (
            f"lost increments: got {slot.reconnect_failures}, expected {expected}"
        )

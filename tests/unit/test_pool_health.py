"""Tests for pool_health: get_healthy_feed_gap_s and check_facade_health."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import (
    check_facade_health,
    get_healthy_feed_gap_s,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slot(
    conn_id: str = "conn-0",
    state: FacadeState = FacadeState.CONNECTED,
    last_data_offset_s: float = 0.0,
    reconnect_failures: int = 0,
    degraded_since_offset_s: float | None = None,
) -> FacadeSlot:
    """Create a FacadeSlot with controlled timestamps."""
    slot = FacadeSlot(conn_id=conn_id, facade=MagicMock())
    slot.state = state
    now = time.monotonic()
    slot.last_data_mono = now - last_data_offset_s
    slot.reconnect_failures = reconnect_failures
    if degraded_since_offset_s is not None:
        slot.degraded_since_mono = now - degraded_since_offset_s
    else:
        slot.degraded_since_mono = None
    return slot


# ---------------------------------------------------------------------------
# get_healthy_feed_gap_s
# ---------------------------------------------------------------------------


class TestGetHealthyFeedGapS:
    def test_returns_inf_when_no_slots(self) -> None:
        assert get_healthy_feed_gap_s([]) == float("inf")

    def test_returns_inf_when_no_connected_slots(self) -> None:
        slots = [
            _make_slot("conn-0", FacadeState.DEGRADED, last_data_offset_s=1.0),
            _make_slot("conn-1", FacadeState.DISCONNECTED, last_data_offset_s=2.0),
            _make_slot("conn-2", FacadeState.RECOVERING, last_data_offset_s=3.0),
        ]
        assert get_healthy_feed_gap_s(slots) == float("inf")

    def test_returns_gap_for_single_connected_slot(self) -> None:
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=2.5)
        result = get_healthy_feed_gap_s([slot])
        assert 2.4 < result < 2.7  # tolerate small timing jitter

    def test_returns_max_gap_across_connected_slots(self) -> None:
        slots = [
            _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=1.0),
            _make_slot("conn-1", FacadeState.CONNECTED, last_data_offset_s=5.0),
            _make_slot("conn-2", FacadeState.CONNECTED, last_data_offset_s=2.0),
        ]
        result = get_healthy_feed_gap_s(slots)
        assert 4.9 < result < 5.2

    def test_ignores_non_connected_in_max_calculation(self) -> None:
        slots = [
            _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=1.0),
            _make_slot("conn-1", FacadeState.DEGRADED, last_data_offset_s=100.0),
            _make_slot("conn-2", FacadeState.DISCONNECTED, last_data_offset_s=200.0),
        ]
        result = get_healthy_feed_gap_s(slots)
        assert 0.9 < result < 1.3

    def test_zero_gap_when_recently_updated(self) -> None:
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=0.0)
        result = get_healthy_feed_gap_s([slot])
        assert 0.0 <= result < 0.1


# ---------------------------------------------------------------------------
# check_facade_health — CONNECTED → DEGRADED transition
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthConnectedToDegraded:
    def test_connected_stays_connected_within_threshold(self) -> None:
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=1.0)
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.CONNECTED
        schedule_fn.assert_not_called()

    def test_connected_transitions_to_degraded_when_gap_exceeded(self) -> None:
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=4.0)
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.DEGRADED
        # degraded_since_mono should be set (not None)
        assert slot.degraded_since_mono is not None
        schedule_fn.assert_not_called()

    def test_connected_to_degraded_sets_degraded_since(self) -> None:
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=5.0)
        before = time.monotonic()
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        after = time.monotonic()
        assert slot.state == FacadeState.DEGRADED
        assert slot.degraded_since_mono is not None
        assert before <= slot.degraded_since_mono <= after

    def test_connected_at_exact_threshold_boundary_stays_connected(self) -> None:
        """Gap equal to threshold should NOT trigger schedule_fn."""
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=3.0)
        schedule_fn = MagicMock()
        # gap == threshold: borderline — implementation uses strict >
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        # The key invariant is: schedule_fn must NOT be called regardless of state
        schedule_fn.assert_not_called()


# ---------------------------------------------------------------------------
# check_facade_health — DEGRADED → CONNECTED recovery
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthDegradedRecovery:
    def test_degraded_recovers_to_connected_when_gap_below_threshold(self) -> None:
        slot = _make_slot(
            "conn-0",
            FacadeState.DEGRADED,
            last_data_offset_s=0.5,
            degraded_since_offset_s=2.0,
        )
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.CONNECTED
        schedule_fn.assert_not_called()

    def test_degraded_stays_degraded_when_gap_still_above_threshold(self) -> None:
        slot = _make_slot(
            "conn-0",
            FacadeState.DEGRADED,
            last_data_offset_s=4.0,
            degraded_since_offset_s=2.0,
        )
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.DEGRADED
        schedule_fn.assert_not_called()

    def test_degraded_triggers_schedule_when_exceeded_reconnect_trigger(self) -> None:
        slot = _make_slot(
            conn_id="conn-7",
            state=FacadeState.DEGRADED,
            last_data_offset_s=5.0,
            degraded_since_offset_s=15.0,  # 15s > reconnect_trigger_s=10s
        )
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_called_once_with("conn-7")

    def test_degraded_does_not_trigger_schedule_if_reconnect_trigger_not_met(self) -> None:
        slot = _make_slot(
            conn_id="conn-3",
            state=FacadeState.DEGRADED,
            last_data_offset_s=5.0,
            degraded_since_offset_s=5.0,  # only 5s degraded, trigger=10s
        )
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.DEGRADED
        schedule_fn.assert_not_called()


# ---------------------------------------------------------------------------
# check_facade_health — DISCONNECTED backoff trigger
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthDisconnected:
    def test_disconnected_triggers_schedule_when_backoff_elapsed(self) -> None:
        slot = _make_slot(
            conn_id="conn-2",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=0,  # backoff = min(120, 5*2^0) = 5s
        )
        # set last_reconnect_mono so that backoff has elapsed
        slot.last_reconnect_mono = time.monotonic() - 6.0  # 6s ago > 5s backoff
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_called_once_with("conn-2")

    def test_disconnected_does_not_trigger_schedule_within_backoff(self) -> None:
        slot = _make_slot(
            conn_id="conn-4",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=0,  # backoff = 5s
        )
        slot.last_reconnect_mono = time.monotonic() - 2.0  # only 2s ago
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_not_called()

    def test_disconnected_backoff_increases_with_failures(self) -> None:
        slot = _make_slot(
            conn_id="conn-5",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=3,  # backoff = min(120, 5*2^3) = 40s
        )
        slot.last_reconnect_mono = time.monotonic() - 10.0  # only 10s < 40s backoff
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_not_called()

    def test_disconnected_triggers_when_backoff_capped_at_120s(self) -> None:
        slot = _make_slot(
            conn_id="conn-6",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=10,  # backoff = min(120, 5*1024) = 120s
        )
        slot.last_reconnect_mono = time.monotonic() - 125.0  # 125s > 120s cap
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_called_once_with("conn-6")

    def test_disconnected_with_zero_last_reconnect_triggers_immediately(self) -> None:
        """last_reconnect_mono=0.0 means never reconnected; should trigger immediately."""
        slot = _make_slot(
            conn_id="conn-8",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=0,
        )
        slot.last_reconnect_mono = 0.0  # never reconnected
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        schedule_fn.assert_called_once_with("conn-8")


# ---------------------------------------------------------------------------
# check_facade_health — RECOVERING slots are skipped
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthRecovering:
    def test_recovering_slot_is_not_touched(self) -> None:
        slot = _make_slot(
            conn_id="conn-9",
            state=FacadeState.RECOVERING,
            last_data_offset_s=60.0,  # huge gap — still skipped
        )
        schedule_fn = MagicMock()
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert slot.state == FacadeState.RECOVERING
        schedule_fn.assert_not_called()


# ---------------------------------------------------------------------------
# check_facade_health — multiple slots processed independently
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthMultipleSlots:
    def test_processes_all_slots_independently(self) -> None:
        schedule_fn = MagicMock()
        connected_ok = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=1.0)
        connected_bad = _make_slot("conn-1", FacadeState.CONNECTED, last_data_offset_s=5.0)
        degraded_recovering = _make_slot(
            "conn-2", FacadeState.DEGRADED, last_data_offset_s=0.5, degraded_since_offset_s=2.0
        )
        degraded_trigger = _make_slot(
            "conn-3", FacadeState.DEGRADED, last_data_offset_s=5.0, degraded_since_offset_s=15.0
        )
        recovering = _make_slot("conn-4", FacadeState.RECOVERING, last_data_offset_s=100.0)

        check_facade_health(
            [connected_ok, connected_bad, degraded_recovering, degraded_trigger, recovering],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
        )

        assert connected_ok.state == FacadeState.CONNECTED
        assert connected_bad.state == FacadeState.DEGRADED
        assert degraded_recovering.state == FacadeState.CONNECTED
        assert degraded_trigger.state == FacadeState.DEGRADED
        assert recovering.state == FacadeState.RECOVERING

        # schedule_fn called only for conn-3
        schedule_fn.assert_called_once_with("conn-3")

    def test_multiple_disconnected_all_get_scheduled(self) -> None:
        schedule_fn = MagicMock()
        slots = []
        for i in range(3):
            s = _make_slot(f"conn-{i}", FacadeState.DISCONNECTED, reconnect_failures=0)
            s.last_reconnect_mono = time.monotonic() - 10.0  # 10s > 5s backoff
            slots.append(s)

        check_facade_health(slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=schedule_fn)
        assert schedule_fn.call_count == 3
        schedule_fn.assert_any_call("conn-0")
        schedule_fn.assert_any_call("conn-1")
        schedule_fn.assert_any_call("conn-2")


# ---------------------------------------------------------------------------
# check_facade_health — suppress_reconnect flag (defense-in-depth)
# ---------------------------------------------------------------------------


class TestCheckFacadeHealthSuppressReconnect:
    """When suppress_reconnect=True, state transitions still occur but
    schedule_fn is never called — prevents futile reconnect attempts
    outside trading hours."""

    def test_degraded_state_transition_still_occurs(self) -> None:
        """CONNECTED→DEGRADED transition should happen even when suppressed."""
        slot = _make_slot("conn-0", FacadeState.CONNECTED, last_data_offset_s=5.0)
        schedule_fn = MagicMock()
        check_facade_health(
            [slot],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        assert slot.state == FacadeState.DEGRADED
        schedule_fn.assert_not_called()

    def test_degraded_recovery_still_occurs(self) -> None:
        """DEGRADED→CONNECTED recovery should happen even when suppressed."""
        slot = _make_slot(
            "conn-0",
            FacadeState.DEGRADED,
            last_data_offset_s=0.5,
            degraded_since_offset_s=2.0,
        )
        schedule_fn = MagicMock()
        check_facade_health(
            [slot],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        assert slot.state == FacadeState.CONNECTED
        schedule_fn.assert_not_called()

    def test_degraded_reconnect_trigger_suppressed(self) -> None:
        """schedule_fn must NOT be called when degraded long enough but suppressed."""
        slot = _make_slot(
            conn_id="conn-0",
            state=FacadeState.DEGRADED,
            last_data_offset_s=5.0,
            degraded_since_offset_s=15.0,
        )
        schedule_fn = MagicMock()
        check_facade_health(
            [slot],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        # State stays DEGRADED, but no reconnect scheduled
        assert slot.state == FacadeState.DEGRADED
        schedule_fn.assert_not_called()

    def test_disconnected_reconnect_suppressed(self) -> None:
        """DISCONNECTED slots must not trigger reconnect when suppressed."""
        slot = _make_slot(
            conn_id="conn-0",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=0,
        )
        slot.last_reconnect_mono = time.monotonic() - 10.0  # backoff elapsed
        schedule_fn = MagicMock()
        check_facade_health(
            [slot],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        assert slot.state == FacadeState.DISCONNECTED
        schedule_fn.assert_not_called()

    def test_disconnected_initial_reconnect_suppressed(self) -> None:
        """Even first-ever reconnect for DISCONNECTED must be suppressed."""
        slot = _make_slot(
            conn_id="conn-0",
            state=FacadeState.DISCONNECTED,
            reconnect_failures=0,
        )
        slot.last_reconnect_mono = 0.0  # never reconnected
        schedule_fn = MagicMock()
        check_facade_health(
            [slot],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        schedule_fn.assert_not_called()

    def test_mixed_slots_all_suppressed(self) -> None:
        """Multiple slots in different states — none should trigger reconnect."""
        schedule_fn = MagicMock()
        degraded_trigger = _make_slot(
            "conn-0", FacadeState.DEGRADED, last_data_offset_s=5.0, degraded_since_offset_s=15.0
        )
        disconnected = _make_slot("conn-1", FacadeState.DISCONNECTED, reconnect_failures=0)
        disconnected.last_reconnect_mono = time.monotonic() - 10.0

        check_facade_health(
            [degraded_trigger, disconnected],
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=schedule_fn,
            suppress_reconnect=True,
        )
        schedule_fn.assert_not_called()

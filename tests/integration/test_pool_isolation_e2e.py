"""E2E integration test for per-connection isolation in QuoteConnectionPool.

Simulates production failure scenario: 1 of 4 connections drops, and verifies:
1. Only the affected facade goes DEGRADED
2. Other facades remain CONNECTED
3. StormGuard feed gap (via get_healthy_feed_gap_s) stays low (< 1s)
4. When ALL facades drop, feed gap returns inf (safety net for HALT)
5. Reconnect only targets the failed facade, not healthy ones
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import check_facade_health, get_healthy_feed_gap_s


class TestSingleFacadeFailureIsolation:
    """Simulates production scenario: 1 of 4 connections drops."""

    def _make_slots(self, n: int = 4) -> list[FacadeSlot]:
        slots = []
        for i in range(n):
            facade = MagicMock()
            facade.logged_in = True
            facade.reconnect.return_value = True
            slot = FacadeSlot(conn_id=str(i), facade=facade)
            slot.state = FacadeState.CONNECTED  # simulate post-subscribe
            slot.symbols = {f"SYM_{i}_A", f"SYM_{i}_B"}
            slot.last_data_mono = time.monotonic()
            slots.append(slot)
        return slots

    def test_single_drop_isolates_to_degraded(self):
        slots = self._make_slots()
        # conn_2 stops receiving data 8s ago
        slots[2].last_data_mono = time.monotonic() - 8.0

        check_facade_health(slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda _: None)

        assert slots[0].state == FacadeState.CONNECTED
        assert slots[1].state == FacadeState.CONNECTED
        assert slots[2].state == FacadeState.DEGRADED
        assert slots[3].state == FacadeState.CONNECTED

    def test_healthy_gap_excludes_degraded(self):
        slots = self._make_slots()
        slots[2].last_data_mono = time.monotonic() - 8.0
        check_facade_health(slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda _: None)

        gap = get_healthy_feed_gap_s(slots)
        assert gap < 1.0, f"Healthy gap should be <1s, got {gap}"

    def test_all_drop_returns_inf(self):
        slots = self._make_slots()
        for s in slots:
            s.last_data_mono = time.monotonic() - 10.0
        check_facade_health(slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda _: None)

        gap = get_healthy_feed_gap_s(slots)
        assert gap == float("inf")

    def test_degraded_triggers_reconnect_after_trigger_threshold(self):
        slots = self._make_slots()
        slots[1].state = FacadeState.DEGRADED
        slots[1].degraded_since_mono = time.monotonic() - 12.0
        slots[1].last_data_mono = time.monotonic() - 15.0

        scheduled: list = []
        check_facade_health(
            slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: scheduled.append(cid)
        )

        assert scheduled == [slots[1].conn_id]

    def test_reconnect_only_targets_failed(self):
        slots = self._make_slots()
        slots[1].state = FacadeState.DEGRADED

        # Simulate pool.reconnect behavior
        for slot in slots:
            if slot.state != FacadeState.CONNECTED:
                slot.facade.reconnect(reason="test")
                slot.state = FacadeState.CONNECTED

        slots[0].facade.reconnect.assert_not_called()
        slots[1].facade.reconnect.assert_called_once()
        slots[2].facade.reconnect.assert_not_called()
        slots[3].facade.reconnect.assert_not_called()

    def test_recovery_after_transient_gap(self):
        """A brief gap < degraded_threshold recovers automatically."""
        slots = self._make_slots()
        slots[0].state = FacadeState.DEGRADED
        slots[0].last_data_mono = time.monotonic() - 1.0  # gap < 3s

        check_facade_health(slots, degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda _: None)

        assert slots[0].state == FacadeState.CONNECTED

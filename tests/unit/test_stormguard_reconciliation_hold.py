"""Tests for StormGuard reconciliation hold mechanism.

Verifies that HALT triggered by RECONCILIATION_MISMATCH does not auto-recover
until ReconciliationService confirms drift is resolved.
"""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
        # Speed up tests: zero cooldown, single clear needed
        g._halt_cooldown_s = 0.0
        g._storm_cooldown_s = 0.0
        g._de_escalate_threshold = 1
        yield g


def test_reconciliation_hold_blocks_halt_recovery(guard):
    """HALT with reconciliation hold must NOT auto-recover via update()."""
    guard.trigger_halt("RECONCILIATION_MISMATCH")
    assert guard.state == StormGuardState.HALT

    guard.set_reconciliation_hold(True)

    # Multiple update() calls with healthy metrics should NOT de-escalate
    for _ in range(10):
        guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)

    assert guard.state == StormGuardState.HALT
    assert guard.reconciliation_hold is True


def test_reconciliation_hold_release_allows_recovery(guard):
    """After reconciliation hold is released, HALT can recover normally."""
    guard.trigger_halt("RECONCILIATION_MISMATCH")
    guard.set_reconciliation_hold(True)

    # Still held
    guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert guard.state == StormGuardState.HALT

    # Release hold
    guard.set_reconciliation_hold(False)

    # Now de-escalation should work
    state = guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert state == StormGuardState.NORMAL


def test_reconciliation_hold_does_not_affect_non_halt(guard):
    """Reconciliation hold only blocks HALT de-escalation, not STORM."""
    guard.trigger_storm("test")
    guard.set_reconciliation_hold(True)

    # STORM should still be able to de-escalate (hold is HALT-specific)
    state = guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert state == StormGuardState.NORMAL


def test_reconciliation_hold_default_false(guard):
    """Reconciliation hold is False by default."""
    assert guard.reconciliation_hold is False


def test_normal_halt_recovery_without_hold(guard):
    """Without reconciliation hold, HALT recovers normally (regression test)."""
    guard.trigger_halt("some other reason")
    assert guard.state == StormGuardState.HALT

    state = guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert state == StormGuardState.NORMAL


def test_reconciliation_hold_set_idempotent(guard):
    """Setting hold to the same value is a no-op (no error)."""
    guard.set_reconciliation_hold(True)
    guard.set_reconciliation_hold(True)  # idempotent
    assert guard.reconciliation_hold is True

    guard.set_reconciliation_hold(False)
    guard.set_reconciliation_hold(False)
    assert guard.reconciliation_hold is False

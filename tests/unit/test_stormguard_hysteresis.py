"""Tests for StormGuard hysteresis (cooldown + N-consecutive-clears de-escalation).

Covers:
- test_escalation_is_instant
- test_deescalation_requires_n_clears
- test_cooldown_blocks_recovery
- test_cooldown_elapsed_allows_recovery
- test_escalation_resets_counter
- test_trigger_halt_bypasses_hysteresis
- test_stormguard_fsm_hysteresis
"""

import os
import time
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


def _make_storm_guard(cooldown_s: float = 30.0, de_n: int = 5) -> StormGuard:
    """Create a StormGuard with test-friendly hysteresis params via env vars."""
    with patch.dict(
        os.environ,
        {
            "HFT_STORMGUARD_STORM_COOLDOWN_S": str(cooldown_s),
            "HFT_STORMGUARD_DE_ESCALATE_N": str(de_n),
        },
    ):
        sg = StormGuard(thresholds=RiskThresholds())
    sg.metrics = MagicMock()
    sg.metrics.stormguard_mode = MagicMock()
    sg.metrics.stormguard_mode.labels.return_value = MagicMock()
    return sg


def test_escalation_is_instant():
    """feed_gap above threshold → single update() call → STORM immediately."""
    sg = _make_storm_guard(cooldown_s=30.0, de_n=5)
    thresholds = sg.thresholds
    assert sg.state == StormGuardState.NORMAL

    state = sg.update(feed_gap_s=thresholds.feed_gap_storm_s + 0.1)
    assert state == StormGuardState.STORM


def test_deescalation_requires_n_clears():
    """After STORM, N-1 clear evals keep state STORM; N-th clear triggers NORMAL."""
    sg = _make_storm_guard(cooldown_s=0.0, de_n=5)

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    for _ in range(4):
        state = sg.update()
        assert state == StormGuardState.STORM, "Should remain STORM before reaching N clears"

    state = sg.update()
    assert state == StormGuardState.NORMAL


def test_cooldown_blocks_recovery():
    """Even after N clear evals, cooldown not elapsed → stays STORM."""
    sg = _make_storm_guard(cooldown_s=9999.0, de_n=1)

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    for _ in range(20):
        state = sg.update()
        assert state == StormGuardState.STORM, "Cooldown should block recovery"


def test_cooldown_elapsed_allows_recovery():
    """After cooldown elapses, N consecutive clears → NORMAL."""
    sg = _make_storm_guard(cooldown_s=0.001, de_n=3)

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    time.sleep(0.01)

    sg._storm_entry_ts = sg._storm_entry_ts - 10.0

    for i in range(3):
        state = sg.update()
        if i < 2:
            assert state == StormGuardState.STORM, f"Should still be STORM after {i + 1} clears"

    assert sg.state == StormGuardState.NORMAL


def test_escalation_resets_counter():
    """Partial de-escalation progress is lost when a new escalation occurs."""
    sg = _make_storm_guard(cooldown_s=0.0, de_n=5)

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    for _ in range(3):
        sg.update()
    assert sg._de_escalate_count == 3

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.5)
    assert sg._de_escalate_count == 0
    assert sg.state == StormGuardState.STORM


def test_trigger_halt_bypasses_hysteresis():
    """trigger_halt() is an immediate override and is not subject to hysteresis."""
    sg = _make_storm_guard(cooldown_s=9999.0, de_n=999)
    sg.trigger_halt("manual override")
    assert sg.state == StormGuardState.HALT

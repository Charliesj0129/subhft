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
from hft_platform.risk.validators import StormGuardFSM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_fsm(cooldown_s: float = 30.0, de_n: int = 5) -> StormGuardFSM:
    config = {
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        }
    }
    with patch.dict(
        os.environ,
        {
            "HFT_STORMGUARD_STORM_COOLDOWN_S": str(cooldown_s),
            "HFT_STORMGUARD_DE_ESCALATE_N": str(de_n),
        },
    ):
        fsm = StormGuardFSM(config)
    fsm.metrics = MagicMock()
    fsm.metrics.stormguard_mode = MagicMock()
    fsm.metrics.stormguard_mode.labels.return_value = MagicMock()
    return fsm


# ---------------------------------------------------------------------------
# StormGuard tests
# ---------------------------------------------------------------------------


def test_escalation_is_instant():
    """feed_gap above threshold → single update() call → STORM immediately."""
    sg = _make_storm_guard(cooldown_s=30.0, de_n=5)
    thresholds = sg.thresholds
    assert sg.state == StormGuardState.NORMAL

    state = sg.update(feed_gap_s=thresholds.feed_gap_storm_s + 0.1)
    assert state == StormGuardState.STORM


def test_deescalation_requires_n_clears():
    """After STORM, N-1 clear evals keep state STORM; N-th clear triggers NORMAL."""
    sg = _make_storm_guard(cooldown_s=0.0, de_n=5)  # cooldown=0 so only N matters

    # Escalate
    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    # N-1 clear evals: should still be STORM
    for _ in range(4):
        state = sg.update()
        assert state == StormGuardState.STORM, "Should remain STORM before reaching N clears"

    # N-th clear eval: should transition to NORMAL
    state = sg.update()
    assert state == StormGuardState.NORMAL


def test_cooldown_blocks_recovery():
    """Even after N clear evals, cooldown not elapsed → stays STORM."""
    sg = _make_storm_guard(cooldown_s=9999.0, de_n=1)  # huge cooldown

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    # Attempt many clears — cooldown not elapsed so counter always resets
    for _ in range(20):
        state = sg.update()
        assert state == StormGuardState.STORM, "Cooldown should block recovery"


def test_cooldown_elapsed_allows_recovery():
    """After cooldown elapses, N consecutive clears → NORMAL."""
    sg = _make_storm_guard(cooldown_s=0.001, de_n=3)  # very short cooldown

    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    # Wait for cooldown
    time.sleep(0.01)

    # Monkeypatch _storm_entry_ts to be far in the past (already done by sleep above)
    sg._storm_entry_ts = sg._storm_entry_ts - 10.0  # push it further back

    for i in range(3):
        state = sg.update()
        if i < 2:
            assert state == StormGuardState.STORM, f"Should still be STORM after {i + 1} clears"

    assert sg.state == StormGuardState.NORMAL


def test_escalation_resets_counter():
    """Partial de-escalation progress is lost when a new escalation occurs."""
    sg = _make_storm_guard(cooldown_s=0.0, de_n=5)

    # Escalate
    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM

    # Make 3 clear evals (counter = 3)
    for _ in range(3):
        sg.update()
    assert sg._de_escalate_count == 3

    # Escalate again (new feed gap spike) → counter must reset
    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.5)
    assert sg._de_escalate_count == 0
    assert sg.state == StormGuardState.STORM


def test_trigger_halt_bypasses_hysteresis():
    """trigger_halt() is an immediate override and is not subject to hysteresis."""
    sg = _make_storm_guard(cooldown_s=9999.0, de_n=999)
    sg.trigger_halt("manual override")
    assert sg.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# StormGuardFSM test (validators.py)
# ---------------------------------------------------------------------------


def test_stormguard_fsm_validate_halt_blocks_new():
    """Unified StormGuard: validate() blocks NEW in HALT, allows CANCEL."""
    from hft_platform.contracts.strategy import IntentType, OrderIntent, Side

    sg = _make_storm_guard(cooldown_s=0.0, de_n=1)
    sg.update(drawdown_bps=-300)  # trigger HALT
    assert sg.state == StormGuardState.HALT

    new_intent = OrderIntent(
        intent_id=1, strategy_id="test", symbol="2330", price=1000000, qty=1,
        side=Side.BUY, intent_type=IntentType.NEW,
    )
    cancel_intent = OrderIntent(
        intent_id=2, strategy_id="test", symbol="2330", price=1000000, qty=1,
        side=Side.BUY, intent_type=IntentType.CANCEL,
    )

    ok, reason = sg.validate(new_intent)
    assert not ok
    assert reason == "STORMGUARD_HALT"

    ok, reason = sg.validate(cancel_intent)
    assert ok


def test_stormguard_fsm_validate_storm_blocks_new():
    """Unified StormGuard: validate() blocks NEW in STORM, allows non-NEW."""
    from hft_platform.contracts.strategy import IntentType, OrderIntent, Side

    sg = _make_storm_guard(cooldown_s=0.0, de_n=1)
    sg.update(drawdown_bps=-150)  # trigger STORM
    assert sg.state == StormGuardState.STORM

    new_intent = OrderIntent(
        intent_id=3, strategy_id="test", symbol="2330", price=1000000, qty=1,
        side=Side.BUY, intent_type=IntentType.NEW,
    )
    ok, reason = sg.validate(new_intent)
    assert not ok
    assert reason == "STORMGUARD_STORM_NEW_BLOCKED"

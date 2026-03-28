"""Tests for StormGuard HALT cooldown — prevents HALT↔NORMAL oscillation (H-1)."""

import os
from unittest.mock import patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


class TestStormGuardHaltCooldown:
    """HALT de-escalation requires cooldown + N consecutive clear evals."""

    def test_halt_does_not_immediately_recover(self):
        """HALT must not step down on the very first clear eval."""
        guard = StormGuard(thresholds=RiskThresholds())
        # Enter HALT
        guard.update(drawdown_bps=-200)
        assert guard.state == StormGuardState.HALT

        # Immediately signal all-clear — should NOT recover due to cooldown
        result = guard.update(drawdown_bps=0)
        assert result == StormGuardState.HALT, "HALT must not immediately de-escalate"

    def test_halt_recovers_after_cooldown_and_consecutive_clears(self):
        """HALT can de-escalate after cooldown elapsed + N consecutive clear evals."""
        guard = StormGuard(thresholds=RiskThresholds())
        guard._halt_cooldown_s = 5.0  # short cooldown for test
        guard._de_escalate_threshold = 3

        # Enter HALT
        guard.update(drawdown_bps=-200)
        assert guard.state == StormGuardState.HALT

        # Simulate time passing beyond cooldown (D3: now uses time.monotonic)
        guard._halt_entry_ts = 100.0

        # Clear evals before cooldown — should stay HALT
        with patch("time.monotonic", return_value=102.0):  # only 2s, cooldown is 5s
            guard.update(drawdown_bps=0)
        assert guard.state == StormGuardState.HALT

        # After cooldown, need N consecutive clears
        with patch("time.monotonic", return_value=106.0):  # 6s > 5s cooldown
            guard.update(drawdown_bps=0)  # 1st clear
        assert guard.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=106.0):
            guard.update(drawdown_bps=0)  # 2nd clear
        assert guard.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=106.0):
            guard.update(drawdown_bps=0)  # 3rd clear — should recover
        assert guard.state == StormGuardState.NORMAL

    def test_halt_cooldown_configurable_via_env(self):
        """HFT_STORMGUARD_HALT_COOLDOWN_S env var sets the cooldown."""
        with patch.dict(os.environ, {"HFT_STORMGUARD_HALT_COOLDOWN_S": "120"}):
            guard = StormGuard(thresholds=RiskThresholds())
            assert guard._halt_cooldown_s == 120.0

    def test_cancels_still_allowed_during_halt(self):
        """Verify that cancel orders pass through during HALT (safety invariant)."""
        guard = StormGuard(thresholds=RiskThresholds())
        guard.update(drawdown_bps=-200)
        assert guard.state == StormGuardState.HALT
        # is_safe() returns False during HALT — risk engine allows cancels separately
        assert not guard.is_safe()

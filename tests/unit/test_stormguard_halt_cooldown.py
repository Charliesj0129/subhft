"""Tests for StormGuard HALT cooldown — prevents HALT↔NORMAL oscillation (H-1)."""

import os
from unittest.mock import patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


class TestStormGuardHaltCooldown:
    """HALT de-escalation requires cooldown + N consecutive clear evals."""

    def test_halt_does_not_immediately_recover(self):
        """HALT must not step down on the very first clear eval."""
        guard = StormGuard(thresholds=RiskThresholds())
        guard.trigger_halt("test halt")
        assert guard.state == StormGuardState.HALT

        result = guard.update()
        assert result == StormGuardState.HALT, "HALT must not immediately de-escalate"

    def test_halt_recovers_after_cooldown_and_consecutive_clears(self):
        """HALT can de-escalate after cooldown elapsed + N consecutive clear evals."""
        guard = StormGuard(thresholds=RiskThresholds())
        guard._halt_cooldown_s = 5.0
        guard._de_escalate_threshold = 3

        guard.trigger_halt("test halt")
        assert guard.state == StormGuardState.HALT

        guard._halt_entry_ts = 100.0

        with patch("time.monotonic", return_value=102.0):
            guard.update()
        assert guard.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=106.0):
            guard.update()
        assert guard.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=106.0):
            guard.update()
        assert guard.state == StormGuardState.HALT

        with patch("time.monotonic", return_value=106.0):
            guard.update()
        assert guard.state == StormGuardState.NORMAL

    def test_halt_cooldown_configurable_via_env(self):
        """HFT_STORMGUARD_HALT_COOLDOWN_S env var sets the cooldown."""
        with patch.dict(os.environ, {"HFT_STORMGUARD_HALT_COOLDOWN_S": "120"}):
            guard = StormGuard(thresholds=RiskThresholds())
            assert guard._halt_cooldown_s == 120.0

    def test_cancels_still_allowed_during_halt(self):
        """Verify that cancel orders pass through during HALT (safety invariant)."""
        guard = StormGuard(thresholds=RiskThresholds())
        guard.trigger_halt("test halt")
        assert guard.state == StormGuardState.HALT
        assert not guard.is_safe()

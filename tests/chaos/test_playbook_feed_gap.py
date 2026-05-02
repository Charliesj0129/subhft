"""Chaos Playbook 3 — Feed Gap >30s.

Simulates prolonged market data feed gaps and verifies StormGuard
escalates through STORM/HALT states, blocks new orders, allows
FORCE_FLAT in HALT, and recovers after feed resumes.
"""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, StormGuardState
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

_MOCK_TIME = 1_000_000.0


@pytest.fixture(autouse=True)
def _patch_storm_externals():
    """Patch MetricsRegistry, audit writer, and timebase for feed gap tests."""
    mock_metrics = MagicMock()
    mock_audit = MagicMock()
    time_val = [_MOCK_TIME]

    def _now_s():
        return time_val[0]

    def _now_ns():
        return int(time_val[0] * 1e9)

    with (
        patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=mock_metrics),
        patch("hft_platform.recorder.audit.get_audit_writer", return_value=mock_audit),
        patch("hft_platform.risk.storm_guard.time") as mock_time,
    ):
        mock_time.monotonic = lambda: time_val[0]
        _patch_storm_externals.advance = lambda s: time_val.__setitem__(0, time_val[0] + s)  # type: ignore[attr-defined]
        yield


def _advance_time(seconds: float) -> None:
    _patch_storm_externals.advance(seconds)  # type: ignore[attr-defined]


@pytest.mark.chaos
class TestPlaybookFeedGap:
    """Chaos tests for feed gap scenario."""

    def test_feed_gap_triggers_storm(self) -> None:
        """Feed gap exceeding threshold triggers STORM state."""
        guard = StormGuard(thresholds=RiskThresholds(feed_gap_storm_s=1.0))

        state = guard.update(feed_gap_s=35.0)

        assert state == StormGuardState.STORM

    def test_prolonged_feed_gap_escalates_to_halt(self) -> None:
        """Large drawdown combined with feed gap escalates to HALT."""
        guard = StormGuard(
            thresholds=RiskThresholds(
                feed_gap_storm_s=1.0,
                halt_drawdown_bps=-200,
            )
        )

        # Feed gap triggers STORM
        guard.update(feed_gap_s=35.0)
        assert guard.state == StormGuardState.STORM

        # Drawdown breaches HALT threshold
        state = guard.update(drawdown_bps=-250)
        assert state == StormGuardState.HALT

    def test_halt_blocks_new_orders(self) -> None:
        """HALT state blocks new order intents via is_safe()."""
        guard = StormGuard()
        guard.trigger_halt("feed_gap_critical")

        assert not guard.is_safe()
        assert guard.state == StormGuardState.HALT

    def test_force_flat_allowed_in_halt(self) -> None:
        """FORCE_FLAT orders are still allowed during HALT state."""
        from hft_platform.contracts.strategy import OrderIntent

        guard = StormGuard()
        guard.trigger_halt("feed_gap_critical")

        intent = MagicMock(spec=OrderIntent)
        intent.intent_type = IntentType.FORCE_FLAT

        allowed, reason = guard.validate(intent)

        assert allowed
        assert reason == "OK"

    def test_recovery_after_feed_resumes(self) -> None:
        """StormGuard de-escalates from STORM to NORMAL after clear conditions."""
        guard = StormGuard(thresholds=RiskThresholds(feed_gap_storm_s=1.0))
        guard._storm_cooldown_s = 0.0
        guard._de_escalate_threshold = 1

        # Escalate to STORM
        guard.update(feed_gap_s=35.0)
        assert guard.state == StormGuardState.STORM

        _advance_time(1.0)

        # Clear conditions
        state = guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert state == StormGuardState.NORMAL
        assert guard.is_safe()

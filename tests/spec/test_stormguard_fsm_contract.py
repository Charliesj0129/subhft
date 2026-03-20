"""Spec tests for StormGuard finite state machine contract.

Verifies escalation, de-escalation, hysteresis, halt callback,
and is_safe() semantics.
"""

from __future__ import annotations

from enum import IntEnum
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


def _make_guard(
    thresholds: RiskThresholds | None = None,
    on_halt_callback: object | None = None,
    storm_cooldown_s: float = 0.0,
    de_escalate_n: int = 1,
) -> StormGuard:
    """Create a StormGuard with mocked metrics and audit dependencies."""
    env = {
        "HFT_STORMGUARD_STORM_COOLDOWN_S": str(storm_cooldown_s),
        "HFT_STORMGUARD_DE_ESCALATE_N": str(de_escalate_n),
    }
    with (
        patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_mr,
        patch(
            "hft_platform.risk.storm_guard.get_audit_writer",
            return_value=MagicMock(),
        ),
        patch.dict("os.environ", env, clear=False),
    ):
        mock_metrics = MagicMock()
        mock_mr.get.return_value = mock_metrics
        guard = StormGuard(
            thresholds=thresholds,
            on_halt_callback=on_halt_callback,
        )
    # Patch transition's audit/metrics calls for all future transitions
    guard.metrics = MagicMock()
    return guard


def _patch_audit() -> object:
    """Return a context manager that patches get_audit_writer."""
    return patch(
        "hft_platform.risk.storm_guard.get_audit_writer",
        return_value=MagicMock(),
    )


# ---------------------------------------------------------------------------
# 0. TestInitialState
# ---------------------------------------------------------------------------


class TestInitialState:
    """New StormGuard always starts in NORMAL state."""

    def test_initial_state_is_normal(self) -> None:
        guard = _make_guard()
        assert guard.state == StormGuardState.NORMAL

    def test_state_is_stormguard_state_enum(self) -> None:
        guard = _make_guard()
        assert isinstance(guard.state, StormGuardState)
        assert isinstance(guard.state, IntEnum)

    def test_all_states_are_valid_enum_values(self) -> None:
        guard = _make_guard()
        valid_states = set(StormGuardState)
        assert guard.state in valid_states

        # Verify after transitions the state remains a valid enum
        with _patch_audit():
            guard.update(drawdown_bps=-50)
            assert guard.state in valid_states
            guard.update(drawdown_bps=-100)
            assert guard.state in valid_states
            guard.update(drawdown_bps=-200)
            assert guard.state in valid_states


# ---------------------------------------------------------------------------
# 1. TestEscalation
# ---------------------------------------------------------------------------


class TestEscalation:
    """NORMAL -> WARM -> STORM -> HALT via drawdown_bps thresholds."""

    def test_normal_to_warm_at_threshold(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-50)
        assert state == StormGuardState.WARM

    def test_normal_to_storm_at_threshold(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-100)
        assert state == StormGuardState.STORM

    def test_normal_to_halt_at_threshold(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-200)
        assert state == StormGuardState.HALT

    def test_warm_to_storm_drawdown(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            guard.update(drawdown_bps=-50)
            assert guard.state == StormGuardState.WARM
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

    def test_storm_to_halt_drawdown(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT

    def test_no_escalation_above_warm_threshold(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-49)
        assert state == StormGuardState.NORMAL

    def test_skip_warm_straight_to_halt(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-200)
        assert state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# 2. TestLatencyEscalation
# ---------------------------------------------------------------------------


class TestLatencyEscalation:
    """latency_us triggers WARM and STORM."""

    def test_latency_warm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(latency_us=5_000)
        assert state == StormGuardState.WARM

    def test_latency_storm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(latency_us=20_000)
        assert state == StormGuardState.STORM

    def test_latency_below_warm_stays_normal(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(latency_us=4_999)
        assert state == StormGuardState.NORMAL

    def test_latency_between_warm_and_storm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(latency_us=10_000)
        assert state == StormGuardState.WARM


# ---------------------------------------------------------------------------
# 3. TestFeedGapEscalation
# ---------------------------------------------------------------------------


class TestFeedGapEscalation:
    """feed_gap_s triggers STORM (not HALT)."""

    def test_feed_gap_triggers_storm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(feed_gap_s=1.0)
        assert state == StormGuardState.STORM

    def test_feed_gap_does_not_trigger_halt(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(feed_gap_s=100.0)
        # Even an extreme feed gap should max out at STORM, not HALT
        assert state == StormGuardState.STORM

    def test_feed_gap_below_threshold_stays_normal(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(feed_gap_s=0.5)
        assert state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 4. TestEscalationPriority
# ---------------------------------------------------------------------------


class TestEscalationPriority:
    """Escalation is always instant (no hysteresis)."""

    def test_escalation_is_instant(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-200)
        # Jumped directly from NORMAL to HALT in one call
        assert state == StormGuardState.HALT

    def test_drawdown_halt_overrides_feed_gap_storm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            # Both signals present; drawdown HALT takes priority
            state = guard.update(drawdown_bps=-200, feed_gap_s=5.0)
        assert state == StormGuardState.HALT

    def test_drawdown_storm_overrides_latency_warm(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            state = guard.update(drawdown_bps=-100, latency_us=5_000)
        assert state == StormGuardState.STORM

    def test_multiple_escalations_take_highest(self) -> None:
        guard = _make_guard()
        with _patch_audit():
            # latency_warm + feed_gap_storm → STORM wins
            state = guard.update(latency_us=5_000, feed_gap_s=2.0)
        assert state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# 5. TestDeEscalation
# ---------------------------------------------------------------------------


class TestDeEscalation:
    """De-escalation from STORM requires cooldown + N consecutive clear evals.

    From HALT, immediate step-down when all signals clear.
    """

    def test_storm_requires_consecutive_clear_evals(self) -> None:
        n = 3
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=n)
        with _patch_audit():
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

            # First N-1 clear evals: still STORM
            for _ in range(n - 1):
                guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.STORM

            # Nth clear eval: de-escalates
            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.NORMAL

    def test_storm_cooldown_must_elapse(self) -> None:
        guard = _make_guard(storm_cooldown_s=30.0, de_escalate_n=1)
        with _patch_audit():
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

            # Clear eval but cooldown not elapsed: stays STORM
            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.STORM

    def test_storm_deescalates_after_cooldown(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=1)
        with _patch_audit():
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

            # cooldown=0 and n=1 → immediate de-escalation
            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.NORMAL

    def test_halt_immediate_stepdown_on_clear(self) -> None:
        # HALT de-escalation is immediate (no hysteresis)
        guard = _make_guard(storm_cooldown_s=999.0, de_escalate_n=999)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT

            # One clear eval → immediate step-down despite large cooldown/N
            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.NORMAL

    def test_interrupted_deescalation_resets_count(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=3)
        with _patch_audit():
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

            # Two clear evals
            guard.update(drawdown_bps=0)
            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.STORM

            # Interrupted by a storm-level signal
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.STORM

            # Need full N again
            for _ in range(2):
                guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.STORM

            guard.update(drawdown_bps=0)
            assert guard.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 5b. TestHaltTerminal
# ---------------------------------------------------------------------------


class TestHaltTerminal:
    """HALT is terminal without explicit reset (via clear update signals)."""

    def test_halt_persists_on_repeated_storm_signals(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=1)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT

            # Storm-level signals do not change HALT state
            guard.update(drawdown_bps=-100)
            assert guard.state == StormGuardState.HALT

    def test_halt_steps_down_on_warm_signal(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=1)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT

            # HALT allows immediate step-down when a lower-severity signal is present
            guard.update(drawdown_bps=-50)
            assert guard.state == StormGuardState.WARM

    def test_halt_requires_clear_signal_to_leave(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=1)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT

            # Only a fully clear signal can exit HALT
            guard.update(drawdown_bps=0)
            assert guard.state != StormGuardState.HALT

    def test_trigger_halt_then_stays_halt_without_clear(self) -> None:
        guard = _make_guard(storm_cooldown_s=0.0, de_escalate_n=1)
        with _patch_audit():
            guard.trigger_halt("manual")
            assert guard.state == StormGuardState.HALT

            # Re-evaluate with HALT-level signal: stays HALT
            guard.update(drawdown_bps=-200)
            assert guard.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# 6. TestIsSafeContract
# ---------------------------------------------------------------------------


class TestIsSafeContract:
    """is_safe() returns True for NORMAL/WARM/STORM, False for HALT."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (StormGuardState.NORMAL, True),
            (StormGuardState.WARM, True),
            (StormGuardState.STORM, True),
            (StormGuardState.HALT, False),
        ],
        ids=["normal", "warm", "storm", "halt"],
    )
    def test_is_safe(self, state: StormGuardState, expected: bool) -> None:
        guard = _make_guard()
        guard.state = state
        assert guard.is_safe() is expected


# ---------------------------------------------------------------------------
# 7. TestTriggerHalt
# ---------------------------------------------------------------------------


class TestTriggerHalt:
    """trigger_halt() always reaches HALT regardless of current state."""

    @pytest.mark.parametrize(
        "initial_state",
        [
            StormGuardState.NORMAL,
            StormGuardState.WARM,
            StormGuardState.STORM,
            StormGuardState.HALT,
        ],
        ids=["from_normal", "from_warm", "from_storm", "from_halt"],
    )
    def test_trigger_halt_from_any_state(self, initial_state: StormGuardState) -> None:
        guard = _make_guard()
        guard.state = initial_state
        with _patch_audit():
            guard.trigger_halt("manual override")
        assert guard.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# 8. TestHaltCallback
# ---------------------------------------------------------------------------


class TestHaltCallback:
    """on_halt_callback fires when entering HALT."""

    def test_callback_fires_on_halt_via_update(self) -> None:
        cb = MagicMock()
        guard = _make_guard(on_halt_callback=cb)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
        cb.assert_called_once()

    def test_callback_fires_on_trigger_halt(self) -> None:
        cb = MagicMock()
        guard = _make_guard(on_halt_callback=cb)
        with _patch_audit():
            guard.trigger_halt("supervisor override")
        cb.assert_called_once()

    def test_callback_not_fired_below_halt(self) -> None:
        cb = MagicMock()
        guard = _make_guard(on_halt_callback=cb)
        with _patch_audit():
            guard.update(drawdown_bps=-100)  # STORM only
        cb.assert_not_called()

    def test_no_callback_is_safe(self) -> None:
        # Guard with no callback should not raise
        guard = _make_guard(on_halt_callback=None)
        with _patch_audit():
            guard.update(drawdown_bps=-200)
        assert guard.state == StormGuardState.HALT

    def test_callback_exception_does_not_propagate(self) -> None:
        cb = MagicMock(side_effect=RuntimeError("boom"))
        guard = _make_guard(on_halt_callback=cb)
        with _patch_audit():
            # Should not raise despite callback error
            guard.update(drawdown_bps=-200)
        assert guard.state == StormGuardState.HALT
        cb.assert_called_once()

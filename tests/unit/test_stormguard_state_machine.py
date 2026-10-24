"""WU-17: Exhaustive StormGuard FSM state machine tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

pytestmark = pytest.mark.usefixtures("order_rtt_single_sample")

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guard(
    thresholds: RiskThresholds | None = None,
    on_halt_callback: Any = None,
    cooldown_s: float = 0.0,
    halt_cooldown_s: float = 0.0,
    de_escalate_n: int = 1,
    warm_cooldown_s: float = 0.0,
) -> StormGuard:
    """Create a StormGuard with env-driven hysteresis overridden for testing.

    P4 (2026-04-28): ``warm_cooldown_s`` (default 0.0) added so existing
    tests that rely on instant WARM->NORMAL de-escalation continue to work.
    The dedicated WARM cooldown enforcement is covered by
    ``test_stormguard_warm_cooldown.py``.
    """
    sg = StormGuard(thresholds=thresholds, on_halt_callback=on_halt_callback)
    sg._storm_cooldown_s = cooldown_s
    sg._halt_cooldown_s = halt_cooldown_s
    sg._warm_cooldown_s = warm_cooldown_s
    sg._de_escalate_threshold = de_escalate_n
    return sg


# ---------------------------------------------------------------------------
# RiskThresholds defaults
# ---------------------------------------------------------------------------


class TestRiskThresholds:
    def test_defaults(self) -> None:
        t = RiskThresholds()
        assert not hasattr(t, "halt_drawdown_bps")  # no PnL rung (B4a)
        assert t.latency_warm_us == 5_000
        assert t.latency_storm_us == 20_000
        assert t.feed_gap_storm_s == 1.0

    def test_custom(self) -> None:
        t = RiskThresholds(latency_warm_us=10, latency_storm_us=20)
        assert t.latency_warm_us == 10


# ---------------------------------------------------------------------------
# State transitions: escalation
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_update_takes_no_pnl_input(self) -> None:
        sg = _guard()
        with pytest.raises(TypeError):
            sg.update(drawdown_bps=-1000)  # type: ignore[call-arg]  # removed input, B4a

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"latency_us": 10_000_000},
            {"order_rtt_us": 60_000_000},
            {"feed_gap_s": 3600.0},
            {"latency_us": 10_000_000, "order_rtt_us": 60_000_000, "feed_gap_s": 3600.0},
        ],
        ids=["loop_lag", "order_rtt", "feed_gap", "all"],
    )
    def test_no_health_input_reaches_halt(self, kwargs: dict) -> None:
        sg = _guard()
        sg.update(**kwargs)
        assert sg.state == StormGuardState.STORM

    def test_latency_warm(self) -> None:
        sg = _guard()
        sg.update(latency_us=5_000)
        assert sg.state == StormGuardState.WARM

    def test_latency_storm(self) -> None:
        sg = _guard()
        sg.update(latency_us=20_000)
        assert sg.state == StormGuardState.STORM

    def test_latency_below_warm(self) -> None:
        sg = _guard()
        sg.update(latency_us=4_999)
        assert sg.state == StormGuardState.NORMAL

    def test_feed_gap_triggers_storm(self) -> None:
        sg = _guard()
        sg.update(feed_gap_s=1.0)
        assert sg.state == StormGuardState.STORM

    def test_feed_gap_below_threshold(self) -> None:
        sg = _guard()
        sg.update(feed_gap_s=0.9)
        assert sg.state == StormGuardState.NORMAL

    def test_escalation_chain(self) -> None:
        sg = _guard()
        sg.update(order_rtt_us=1_500_000)
        assert sg.state == StormGuardState.WARM
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT

    def test_escalation_is_instant(self) -> None:
        """Escalation should not require cooldown or consecutive checks."""
        sg = _guard(cooldown_s=9999, de_escalate_n=9999)
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT

    def test_skip_states_normal_to_halt(self) -> None:
        sg = _guard()
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT

    def test_skip_states_normal_to_storm(self) -> None:
        sg = _guard()
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# State transitions: de-escalation with hysteresis
# ---------------------------------------------------------------------------


class TestDeEscalation:
    def test_halt_recovery_with_zero_cooldown(self) -> None:
        """HALT with zero halt_cooldown allows de-escalation after N clears."""
        sg = _guard(cooldown_s=9999, halt_cooldown_s=0, de_escalate_n=1)
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_storm_requires_n_consecutive_clears(self) -> None:
        sg = _guard(cooldown_s=0, de_escalate_n=3)
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # 1st clear
        sg.update()
        assert sg.state == StormGuardState.STORM
        # 2nd clear
        sg.update()
        assert sg.state == StormGuardState.STORM
        # 3rd clear => de-escalate
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_storm_counter_reset_on_re_escalation(self) -> None:
        sg = _guard(cooldown_s=0, de_escalate_n=3)
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # 2 clears, then re-trigger
        sg.update()
        sg.update()
        sg.update(order_rtt_us=2_500_000)  # same state, resets counter
        assert sg.state == StormGuardState.STORM

        # Need full 3 again
        sg.update()
        assert sg.state == StormGuardState.STORM
        sg.update()
        assert sg.state == StormGuardState.STORM
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_warm_de_escalation_no_cooldown_needed(self) -> None:
        """WARM->NORMAL de-escalation respects the WARM cooldown (default 30s).

        Pre-P4 (2026-04-28) behaviour: WARM was the only elevated state
        without a cooldown, so NORMAL<->WARM could oscillate freely. The
        production bug (EXFE6 flap, three cycles in <1hr) was rooted in
        that gap. Post-P4 the cooldown applies; this test sets it to 0
        explicitly to preserve the original "instant de-escalation" intent
        while documenting the new gate.
        """
        sg = _guard(cooldown_s=0, de_escalate_n=1, warm_cooldown_s=0.0)
        sg.update(order_rtt_us=1_500_000)
        assert sg.state == StormGuardState.WARM
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_storm_cooldown_blocks_de_escalation(self) -> None:
        """When cooldown hasn't elapsed, de-escalation counter resets."""
        sg = _guard(cooldown_s=9999, de_escalate_n=1)
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # Try to de-escalate -- cooldown hasn't elapsed
        sg.update()
        assert sg.state == StormGuardState.STORM

    def test_storm_cooldown_elapsed_allows_de_escalation(self) -> None:
        sg = _guard(cooldown_s=30.0, de_escalate_n=1)

        # Enter STORM at monotonic=1000
        with patch("time.monotonic", return_value=1000.0):
            sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # Advance time past cooldown (D3: now uses time.monotonic)
        with patch("time.monotonic", return_value=1031.0):
            sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_halt_to_warm_with_zero_cooldown(self) -> None:
        """HALT with zero halt_cooldown can step down to WARM after N clears."""
        sg = _guard(cooldown_s=9999, halt_cooldown_s=0, de_escalate_n=1)
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT
        sg.update(order_rtt_us=1_500_000)
        assert sg.state == StormGuardState.WARM

    def test_halt_to_storm_with_zero_cooldown(self) -> None:
        sg = _guard(cooldown_s=9999, halt_cooldown_s=0, de_escalate_n=1)
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# Manual HALT: trigger_halt()
# ---------------------------------------------------------------------------


class TestTriggerHalt:
    def test_trigger_halt_from_normal(self) -> None:
        sg = _guard()
        sg.trigger_halt("manual")
        assert sg.state == StormGuardState.HALT

    def test_trigger_halt_from_warm(self) -> None:
        sg = _guard()
        sg.update(order_rtt_us=1_500_000)
        sg.trigger_halt("supervisor")
        assert sg.state == StormGuardState.HALT

    def test_trigger_halt_from_storm(self) -> None:
        sg = _guard()
        sg.update(order_rtt_us=2_500_000)
        sg.trigger_halt("kill_switch")
        assert sg.state == StormGuardState.HALT

    def test_trigger_halt_idempotent(self) -> None:
        sg = _guard()
        sg.trigger_halt("first")
        sg.trigger_halt("second")
        assert sg.state == StormGuardState.HALT

    def test_trigger_halt_callback_fires(self) -> None:
        cb = MagicMock()
        sg = _guard(on_halt_callback=cb)
        sg.trigger_halt("test")
        cb.assert_called_once()

    def test_trigger_halt_callback_exception_handled(self) -> None:
        cb = MagicMock(side_effect=RuntimeError("boom"))
        sg = _guard(on_halt_callback=cb)
        sg.trigger_halt("test")  # should not raise
        assert sg.state == StormGuardState.HALT


# ---------------------------------------------------------------------------
# is_safe()
# ---------------------------------------------------------------------------


class TestIsSafe:
    @pytest.mark.parametrize(
        "state, expected",
        [
            (StormGuardState.NORMAL, True),
            (StormGuardState.WARM, True),
            (StormGuardState.STORM, True),
            (StormGuardState.HALT, False),
        ],
    )
    def test_is_safe(self, state: StormGuardState, expected: bool) -> None:
        sg = _guard()
        sg.state = state
        assert sg.is_safe() is expected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_drawdown(self) -> None:
        sg = _guard()
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_positive_drawdown(self) -> None:
        sg = _guard()
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_extreme_negative_drawdown(self) -> None:
        sg = _guard()
        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT

    def test_latency_zero(self) -> None:
        sg = _guard()
        sg.update(latency_us=0)
        assert sg.state == StormGuardState.NORMAL

    def test_combined_drawdown_and_latency(self) -> None:
        """When both signals trigger, higher severity wins."""
        sg = _guard()
        sg.update(order_rtt_us=1_500_000, latency_us=20_000)
        # drawdown -> WARM, latency -> STORM; STORM should win
        assert sg.state == StormGuardState.STORM

    def test_feed_gap_does_not_halt(self) -> None:
        """Feed gap triggers STORM, not HALT."""
        sg = _guard()
        sg.update(feed_gap_s=999.0)
        assert sg.state == StormGuardState.STORM

    def test_feed_gap_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "5.0")
        sg = StormGuard(thresholds=RiskThresholds())
        sg._storm_cooldown_s = 0.0
        sg._de_escalate_threshold = 1
        assert sg.thresholds.feed_gap_storm_s == 5.0
        sg.update(feed_gap_s=4.9)
        assert sg.state == StormGuardState.NORMAL
        sg.update(feed_gap_s=5.0)
        assert sg.state == StormGuardState.STORM

    def test_invalid_feed_gap_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "not_a_number")
        sg = StormGuard(thresholds=RiskThresholds())
        # Should fall back to default
        assert sg.thresholds.feed_gap_storm_s == 1.0

    def test_last_state_change_updated(self) -> None:
        sg = _guard()
        initial_ts = sg.last_state_change
        sg.trigger_halt("test halt")
        assert sg.last_state_change >= initial_ts

    def test_update_returns_current_state(self) -> None:
        sg = _guard()
        result = sg.update(order_rtt_us=2_500_000)
        assert result == sg.state
        assert result == StormGuardState.STORM


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_cooldown_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "60")
        sg = StormGuard()
        assert sg._storm_cooldown_s == 60.0

    def test_de_escalate_n_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_STORMGUARD_DE_ESCALATE_N", "10")
        sg = StormGuard()
        assert sg._de_escalate_threshold == 10


# ---------------------------------------------------------------------------
# Multi-step scenarios
# ---------------------------------------------------------------------------


class TestMultiStep:
    def test_full_cycle_escalate_and_recover(self) -> None:
        sg = _guard(cooldown_s=0, halt_cooldown_s=0, de_escalate_n=1)
        assert sg.state == StormGuardState.NORMAL

        sg.update(order_rtt_us=1_500_000)
        assert sg.state == StormGuardState.WARM

        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        sg.trigger_halt("test halt")
        assert sg.state == StormGuardState.HALT

        # Recovery from HALT is immediate
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # De-escalate from STORM (cooldown=0, n=1)
        sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_repeated_normal_updates_stay_normal(self) -> None:
        sg = _guard()
        for _ in range(20):
            sg.update()
        assert sg.state == StormGuardState.NORMAL

    def test_oscillating_drawdown(self) -> None:
        """Rapidly alternating drawdown should respect hysteresis."""
        sg = _guard(cooldown_s=0, de_escalate_n=3)
        sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM

        # Oscillate: 2 clears then re-trigger
        for _ in range(5):
            sg.update()
            sg.update()
            sg.update(order_rtt_us=2_500_000)
        assert sg.state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# Property-based (hypothesis)
# ---------------------------------------------------------------------------


if HAS_HYPOTHESIS:

    class TestPropertyBased:
        @given(latency=st.integers(min_value=0, max_value=100_000))
        @settings(max_examples=100)
        def test_monotonic_escalation(self, latency: int) -> None:
            """State should be monotonically non-decreasing with worsening loop lag."""
            sg = _guard()
            sg.update(latency_us=latency)
            state1 = sg.state
            sg2 = _guard()
            sg2.update(latency_us=latency + 1)
            assert sg2.state >= state1

        @given(latency=st.integers(min_value=0, max_value=100_000))
        @settings(max_examples=100)
        def test_is_safe_consistency(self, latency: int) -> None:
            """is_safe() must be False if and only if state is HALT."""
            sg = _guard()
            sg.update(latency_us=latency)
            assert sg.is_safe() == (sg.state < StormGuardState.HALT)

        @given(
            latency=st.integers(min_value=0, max_value=100_000),
            rtt=st.integers(min_value=0, max_value=5_000_000),
        )
        @settings(max_examples=50)
        def test_higher_severity_wins(self, latency: int, rtt: int) -> None:
            """The resulting state should be >= max of individual triggers."""
            sg1 = _guard()
            sg1.update(order_rtt_us=rtt)
            state_rtt = sg1.state

            sg2 = _guard()
            sg2.update(latency_us=latency)
            state_lat = sg2.state

            sg3 = _guard()
            sg3.update(order_rtt_us=rtt, latency_us=latency)
            assert sg3.state >= max(state_rtt, state_lat)

        @given(latency=st.integers(min_value=0, max_value=100_000))
        @settings(max_examples=50)
        def test_trigger_halt_always_halts(self, latency: int) -> None:
            """trigger_halt() should result in HALT regardless of initial state."""
            sg = _guard()
            sg.update(latency_us=latency)
            sg.trigger_halt("test")
            assert sg.state == StormGuardState.HALT
            assert sg.is_safe() is False

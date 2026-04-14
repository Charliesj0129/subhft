"""Tests for PlatformDegradeController shadow mode bypass + singleton env wiring."""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.platform_degrade import (
    _AUTO_RECOVERABLE_REASONS,
    PlatformDegradeController,
    get_shared_platform_degrade_controller,
    reset_shared_platform_degrade_controller,
)


class TestShadowModeBypass:
    def test_shadow_mode_allows_new_opening_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=True)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is True

    def test_shadow_mode_allows_all_intent_types_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=True)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        for itype in (IntentType.NEW, IntentType.CANCEL, IntentType.AMEND, IntentType.FORCE_FLAT):
            assert ctrl.allow_intent(intent_type=itype, opens_risk=True) is True

    def test_non_shadow_blocks_new_opening_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=False)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is False

    def test_non_shadow_allows_cancel_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=False)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.CANCEL, opens_risk=True) is True


class TestSingletonEnvWiring:
    def setup_method(self):
        reset_shared_platform_degrade_controller()

    def teardown_method(self):
        reset_shared_platform_degrade_controller()

    def test_singleton_reads_shadow_mode_from_env(self, monkeypatch):
        monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl._shadow_mode is True

    def test_singleton_defaults_shadow_mode_off(self, monkeypatch):
        monkeypatch.delenv("HFT_ORDER_SHADOW_MODE", raising=False)
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl._shadow_mode is False

    def test_singleton_explicit_shadow_mode_overrides_env(self, monkeypatch):
        monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
        ctrl = get_shared_platform_degrade_controller(shadow_mode=False)
        assert ctrl._shadow_mode is False


class TestActiveReasons:
    def test_reasons_accumulate_on_multiple_entries(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        assert ctrl._active_reasons == {"feed_reconnect_unhealthy", "reconciliation_drift"}

    def test_first_entry_activates_reduce_only(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.reduce_only_active is True

    def test_second_entry_stays_active_adds_reason(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        assert ctrl.reduce_only_active is True
        assert len(ctrl._active_reasons) == 2

    def test_exit_clears_all_reasons(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        ctrl.exit_reduce_only(reason="operator_manual")
        assert ctrl._active_reasons == set()
        assert ctrl.reduce_only_active is False

    def test_duplicate_reason_not_double_counted(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl._active_reasons == {"feed_reconnect_unhealthy"}


class TestAutoRecovery:
    def test_auto_recovery_after_all_reasons_cleared(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Reasons cleared, start cooldown
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert ctrl.reduce_only_active is True  # still in cooldown
        # Cooldown elapses (60s = 60_000_000_000 ns)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=61_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False

    def test_auto_recovery_blocked_by_non_recoverable_reason(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="queue_depth_exceeded")
        # Feed clears but queue_depth_exceeded remains (truly non-recoverable)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert recovered is False
        # Even after cooldown
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=61_000_000_001)
        assert recovered is False
        assert ctrl.reduce_only_active is True

    def test_auto_recovery_reset_on_retrigger(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Reasons cleared, start cooldown at t=1s
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        # Reason re-appears at t=30s
        ctrl.check_auto_recovery(current_reasons=["feed_reconnect_unhealthy"], now_ns=30_000_000_000)
        # Clears again at t=50s
        ctrl.check_auto_recovery(current_reasons=[], now_ns=50_000_000_000)
        # 60s from t=50s would be t=110s — should NOT recover at t=70s
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=70_000_000_000)
        assert recovered is False
        # Should recover at t=111s (60s after re-clear)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=111_000_000_000)
        assert recovered is True

    def test_auto_recovery_disabled(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=False, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=999_000_000_000)
        assert recovered is False
        assert ctrl.reduce_only_active is True

    def test_auto_recovery_clears_auto_recoverable_reasons_from_set(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=1)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Input no longer reports feed_reconnect_unhealthy
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert "feed_reconnect_unhealthy" not in ctrl._active_reasons

    def test_auto_recovery_not_triggered_when_not_active(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=1)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=999_000_000_000)
        assert recovered is False

    def test_auto_recoverable_reasons_include_transient_conditions(self):
        assert "feed_reconnect_unhealthy" in _AUTO_RECOVERABLE_REASONS
        assert "feed_reconnect_pending" in _AUTO_RECOVERABLE_REASONS
        assert "feed_gap_exceeded" in _AUTO_RECOVERABLE_REASONS
        assert "rss_unhealthy" in _AUTO_RECOVERABLE_REASONS
        # Transient conditions that self-resolve must be auto-recoverable
        assert "reconciliation_drift" in _AUTO_RECOVERABLE_REASONS
        assert "feed_reconnect_flapping" in _AUTO_RECOVERABLE_REASONS

    def test_reconciliation_drift_auto_recovers_when_drift_resolves(self):
        """reconciliation_drift enters REDUCE_ONLY but must auto-recover
        once reconciliation stops re-firing the reason.

        Production scenario: day session non-critical drift → REDUCE_ONLY →
        night session opens with positions aligned → should auto-recover,
        not lock the entire night session.
        """
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=10)
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        assert ctrl.reduce_only_active is True

        # Drift resolved — reason no longer in current_reasons
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert "reconciliation_drift" not in ctrl._active_reasons

        # Cooldown elapses
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=11_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False

    def test_feed_reconnect_flapping_auto_recovers_after_flap_window(self):
        """feed_reconnect_flapping must auto-recover after flap events expire.

        Production scenario: reconnect triggers quote flaps → REDUCE_ONLY →
        flap budget resets after window → should auto-recover.
        """
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=10)
        ctrl.enter_reduce_only(reason="feed_reconnect_flapping")
        assert ctrl.reduce_only_active is True

        # Flap events expired — reason no longer in current_reasons
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert "feed_reconnect_flapping" not in ctrl._active_reasons

        # Cooldown elapses
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=11_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False

    def test_night_session_lockup_scenario(self):
        """Simulates the observed production bug: day-session drift + reconnect
        flapping → entire night session locked in REDUCE_ONLY with zero intents.
        """
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=10)
        # Day session: non-critical drift fires
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        # Reconnect also flaps during day→night transition
        ctrl.enter_reduce_only(reason="feed_reconnect_flapping")
        assert ctrl._active_reasons == {"reconciliation_drift", "feed_reconnect_flapping"}

        # Night session opens: both conditions resolve
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert ctrl._active_reasons == set()

        # After cooldown: system recovers
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=11_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False
        # New opening intents should be allowed
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is True

    def test_auto_recovery_works_for_feed_reconnect_pending(self):
        """feed_reconnect_pending must auto-recover after reconnect succeeds."""
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_pending")
        assert ctrl.reduce_only_active is True
        # Reconnect succeeds → reason clears from inputs
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert "feed_reconnect_pending" not in ctrl._active_reasons
        # Cooldown elapses
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=61_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False

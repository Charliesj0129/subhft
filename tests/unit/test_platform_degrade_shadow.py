"""Tests for PlatformDegradeController shadow mode bypass + singleton env wiring."""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.platform_degrade import (
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

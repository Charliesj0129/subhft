import unittest
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.storm_guard import StormGuard, StormGuardState


class TestStormGuard(unittest.TestCase):
    def setUp(self):
        self.temp_metrics = MagicMock()
        # Mock MetricsRegistry.get() to avoid side effects
        patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=self.temp_metrics)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.guard = StormGuard()

    def test_initial_state(self):
        self.assertEqual(self.guard.state, StormGuardState.NORMAL)

    def test_transition_to_warm(self):
        # Drawdown -60 bps (< -50 bps) -> WARM
        state = self.guard.update(drawdown_bps=-60)
        self.assertEqual(state, StormGuardState.WARM)
        self.assertEqual(self.guard.state, StormGuardState.WARM)

    def test_transition_to_storm(self):
        # Latency 21000 (> 20000) -> STORM
        state = self.guard.update(latency_us=21000)
        self.assertEqual(state, StormGuardState.STORM)

    def test_transition_to_halt(self):
        # Feed Gap 1.1 (> 1.0) -> STORM (should not HALT)
        state = self.guard.update(feed_gap_s=1.1)
        self.assertEqual(state, StormGuardState.STORM)

    def test_priority(self):
        # Halt condition AND Storm condition -> Should be HALT
        state = self.guard.update(drawdown_bps=-1000, latency_us=20000)
        self.assertEqual(state, StormGuardState.HALT)

    def test_manual_halt(self):
        self.guard.trigger_halt("Manual")
        self.assertEqual(self.guard.state, StormGuardState.HALT)
        self.assertFalse(self.guard.is_safe())

    def test_recovery(self):
        self.guard.trigger_halt("Manual")
        # With default halt_cooldown, bypass by setting cooldown=0 and n=1
        self.guard._halt_cooldown_s = 0.0
        self.guard._de_escalate_threshold = 1
        # Update with safe values
        state = self.guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0)
        self.assertEqual(state, StormGuardState.NORMAL)
        self.assertTrue(self.guard.is_safe())


# ---------------------------------------------------------------------------
# pytest-style tests for uncovered branches
# ---------------------------------------------------------------------------


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        yield StormGuard()


# ── reload_thresholds ───────────────────────────────────────────────────────


def test_reload_thresholds_updates_risk_sub_dict(guard):
    config = {
        "risk": {
            "warm_drawdown_bps": -30,
            "storm_drawdown_bps": -80,
            "halt_drawdown_bps": -150,
            "latency_warm_us": 3000,
            "latency_storm_us": 10000,
            "feed_gap_storm_s": 2.5,
        }
    }
    guard.reload_thresholds(config)
    assert guard.thresholds.warm_drawdown_bps == -30
    assert guard.thresholds.storm_drawdown_bps == -80
    assert guard.thresholds.halt_drawdown_bps == -150
    assert guard.thresholds.latency_warm_us == 3000
    assert guard.thresholds.latency_storm_us == 10000
    assert guard.thresholds.feed_gap_storm_s == 2.5


def test_reload_thresholds_falls_back_to_global_defaults_key(guard):
    config = {"global_defaults": {"warm_drawdown_bps": -25}}
    guard.reload_thresholds(config)
    assert guard.thresholds.warm_drawdown_bps == -25


def test_reload_thresholds_ignores_unknown_keys(guard):
    """reload_thresholds with no matching keys leaves thresholds unchanged."""
    original_warm = guard.thresholds.warm_drawdown_bps
    guard.reload_thresholds({"risk": {"unknown_key": 999}})
    assert guard.thresholds.warm_drawdown_bps == original_warm


# ── _apply_env_overrides ────────────────────────────────────────────────────


def test_apply_env_override_feed_gap(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "5.0")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.feed_gap_storm_s == 5.0


def test_apply_env_override_feed_gap_invalid_value_logs_warning(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "not_a_float")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        # Should not raise; invalid value is ignored and a warning is logged
        g = StormGuard()
    # Default value remains unchanged (ValueError path taken)
    assert g.thresholds.feed_gap_storm_s == 1.0


# ── canonical HFT_STORMGUARD_FEED_GAP_STORM_S env var ──────────────────────


def test_feed_gap_storm_env_override(monkeypatch):
    """Canonical HFT_STORMGUARD_FEED_GAP_STORM_S sets feed_gap_storm_s correctly."""
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_STORM_S", "45")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.feed_gap_storm_s == 45.0


def test_feed_gap_deprecated_halt_env_still_works(monkeypatch, capfd):
    """Deprecated HFT_STORMGUARD_FEED_GAP_HALT_S is applied and a deprecation warning is logged."""
    monkeypatch.delenv("HFT_STORMGUARD_FEED_GAP_STORM_S", raising=False)
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "60")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    # Deprecated alias is still honoured
    assert g.thresholds.feed_gap_storm_s == 60.0


def test_feed_gap_storm_env_takes_precedence(monkeypatch):
    """When both env vars are set, HFT_STORMGUARD_FEED_GAP_STORM_S wins."""
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_STORM_S", "45")
    monkeypatch.setenv("HFT_STORMGUARD_FEED_GAP_HALT_S", "99")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    # STORM_S should win over the deprecated HALT_S
    assert g.thresholds.feed_gap_storm_s == 45.0


def test_apply_env_override_latency_storm(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "15000")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.latency_storm_us == 15000


def test_apply_env_override_latency_storm_invalid(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_STORM_US", "bad")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.latency_storm_us == 20000  # default unchanged


def test_apply_env_override_latency_warm(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_WARM_US", "2500")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.latency_warm_us == 2500


def test_apply_env_override_latency_warm_invalid(monkeypatch):
    monkeypatch.setenv("HFT_STORMGUARD_LATENCY_WARM_US", "bad")
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g.thresholds.latency_warm_us == 5000  # default unchanged


# ── _evaluate_target_state edge cases ──────────────────────────────────────


def test_evaluate_target_state_storm_drawdown_path(guard):
    """Drawdown between storm and halt threshold triggers STORM, not HALT."""
    state = guard.update(drawdown_bps=-110)  # storm=-100, halt=-200
    assert state == StormGuardState.STORM


def test_evaluate_target_state_warm_latency_path(guard):
    """Latency between warm and storm threshold triggers WARM."""
    state = guard.update(latency_us=6000)  # warm=5000, storm=20000
    assert state == StormGuardState.WARM


# ── de-escalation with cooldown (STORM path) ───────────────────────────────


def test_de_escalation_blocked_during_storm_cooldown(guard):
    """De-escalation count resets to 0 if cooldown not yet elapsed."""
    guard._storm_cooldown_s = 9999.0  # effectively infinite cooldown
    guard._de_escalate_threshold = 1
    guard.update(latency_us=25000)  # enter STORM
    assert guard.state == StormGuardState.STORM
    # Attempt recovery before cooldown expires
    guard.update(latency_us=0)
    assert guard.state == StormGuardState.STORM
    assert guard._de_escalate_count == 0  # reset because cooldown not met


def test_de_escalation_allowed_after_storm_cooldown(guard):
    """De-escalation succeeds once cooldown has elapsed."""
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 2
    guard.update(latency_us=25000)  # enter STORM
    assert guard.state == StormGuardState.STORM
    guard.update(latency_us=0)  # count = 1, not yet threshold
    assert guard.state == StormGuardState.STORM
    guard.update(latency_us=0)  # count = 2 → transition
    assert guard.state == StormGuardState.NORMAL


def test_de_escalation_count_not_incremented_when_staying_in_storm(guard):
    """When state stays in STORM, de-escalate count resets."""
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 5
    guard.update(latency_us=25000)  # enter STORM
    # Keep sending storm conditions — count should stay 0
    guard.update(latency_us=25000)
    assert guard._de_escalate_count == 0


# ── validate method ─────────────────────────────────────────────────────────


def _make_intent(intent_type: IntentType) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=5000000,
        qty=1,
    )


def test_validate_new_order_blocked_during_halt(guard):
    guard.trigger_halt("test")
    ok, reason = guard.validate(_make_intent(IntentType.NEW))
    assert not ok
    assert reason == "STORMGUARD_HALT"


def test_validate_cancel_allowed_during_halt(guard):
    guard.trigger_halt("test")
    ok, reason = guard.validate(_make_intent(IntentType.CANCEL))
    assert ok
    assert reason == "OK"


def test_validate_force_flat_allowed_during_halt(guard):
    guard.trigger_halt("test")
    ok, reason = guard.validate(_make_intent(IntentType.FORCE_FLAT))
    assert ok
    assert reason == "OK"


def test_validate_new_order_blocked_during_storm(guard):
    guard.update(latency_us=25000)  # enter STORM
    assert guard.state == StormGuardState.STORM
    ok, reason = guard.validate(_make_intent(IntentType.NEW))
    assert not ok
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_validate_cancel_allowed_during_storm(guard):
    guard.update(latency_us=25000)
    ok, reason = guard.validate(_make_intent(IntentType.CANCEL))
    assert ok


def test_validate_any_order_allowed_during_normal(guard):
    assert guard.state == StormGuardState.NORMAL
    ok, reason = guard.validate(_make_intent(IntentType.NEW))
    assert ok
    assert reason == "OK"


# ── on_halt_callback ────────────────────────────────────────────────────────


def test_halt_callback_sync_called_on_halt(guard):
    called = []

    def cb():
        called.append(True)

    guard._on_halt_callback = cb
    guard.trigger_halt("cb test")
    assert called == [True]


def test_halt_callback_exception_does_not_propagate(guard):
    def bad_cb():
        raise RuntimeError("oops")

    guard._on_halt_callback = bad_cb
    # Should not raise
    guard.trigger_halt("cb error test")
    assert guard.state == StormGuardState.HALT


def test_halt_callback_coroutine_no_running_loop(guard):
    """Coroutine callback when no event loop is running logs warning, does not crash."""

    async def async_cb():
        pass  # pragma: no cover

    guard._on_halt_callback = async_cb
    guard.trigger_halt("async no loop")
    assert guard.state == StormGuardState.HALT


# ── transition audit failure ────────────────────────────────────────────────


def test_transition_audit_failure_does_not_propagate(guard):
    """If audit writer raises, transition should complete without error."""
    with patch(
        "hft_platform.recorder.audit.get_audit_writer",
        side_effect=RuntimeError("audit unavailable"),
    ):
        # Should not raise — audit is best-effort
        guard._transition(StormGuardState.WARM, "test reason")
    assert guard.state == StormGuardState.WARM


# ── update_with_lob ─────────────────────────────────────────────────────────


class _FakeToxicityResult(NamedTuple):
    burst_detected: bool
    toxicity_score: float
    burst_event: object


def _guard_with_detector(toxicity: float, burst: bool = False):
    mock_metrics = MagicMock()
    mock_detector = MagicMock()
    mock_detector.evaluate.return_value = _FakeToxicityResult(
        burst_detected=burst,
        toxicity_score=toxicity,
        burst_event=None,
    )
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=mock_metrics):
        g = StormGuard(drift_burst_detector=mock_detector)
    return g


def test_update_with_lob_no_detector_returns_current_state(guard):
    assert guard._drift_burst_detector is None
    result = guard.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.NORMAL


def test_update_with_lob_low_toxicity_no_escalation():
    g = _guard_with_detector(toxicity=0.3)
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.NORMAL


def test_update_with_lob_warm_toxicity_escalates_to_warm():
    g = _guard_with_detector(toxicity=0.6)
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.WARM


def test_update_with_lob_storm_toxicity_escalates_to_storm():
    g = _guard_with_detector(toxicity=0.85)
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.STORM


def test_update_with_lob_halt_requires_burst_and_high_toxicity():
    g = _guard_with_detector(toxicity=0.95, burst=True)
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.HALT


def test_update_with_lob_high_toxicity_no_burst_escalates_to_storm():
    """toxicity > 0.9 without burst_detected → STORM, not HALT."""
    g = _guard_with_detector(toxicity=0.95, burst=False)
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.STORM


def test_update_with_lob_crossed_book_escalates_to_storm():
    """Crossed book (spread < 0) must escalate to STORM immediately."""
    g = _guard_with_detector(toxicity=0.1)  # low toxicity — but crossed book overrides
    result = g.update_with_lob(mid_price_x2=2000000, spread_scaled=-100)
    assert result == StormGuardState.STORM


def test_update_with_lob_empty_book_skips_detector():
    """Empty book (mid_price_x2 <= 0) must skip DriftBurst and return current state."""
    g = _guard_with_detector(toxicity=0.95, burst=True)
    result = g.update_with_lob(mid_price_x2=0, spread_scaled=0)
    # Should NOT escalate despite high toxicity detector — empty book skips it
    assert result == StormGuardState.NORMAL


def test_update_with_lob_does_not_de_escalate():
    """update_with_lob is additive-only; it cannot lower the current state."""
    g = _guard_with_detector(toxicity=0.3)
    g.trigger_halt("manual halt")
    result = g.update_with_lob(mid_price_x2=1000000, spread_scaled=100)
    assert result == StormGuardState.HALT


# ── halt_exempt_strategies ──────────────────────────────────────────────────


def _make_intent_with_strategy(intent_type: IntentType, strategy_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="TMFD6",
        intent_type=intent_type,
        side=Side.BUY,
        price=5000000,
        qty=1,
    )


@pytest.fixture
def exempt_guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        return StormGuard(halt_exempt_strategies=frozenset({"spike_fader"}))


def test_exempt_strategy_bypasses_halt(exempt_guard):
    """Exempt strategy can place NEW orders during HALT."""
    exempt_guard.trigger_halt("drift_burst")
    assert exempt_guard.state == StormGuardState.HALT
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert ok
    assert reason == "HALT_EXEMPT"


def test_non_exempt_strategy_still_blocked_during_halt(exempt_guard):
    """Non-exempt strategy is still blocked during HALT."""
    exempt_guard.trigger_halt("drift_burst")
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "cbs_tmfd6"))
    assert not ok
    assert reason == "STORMGUARD_HALT"


def test_exempt_strategy_bypasses_storm(exempt_guard):
    """Exempt strategy can place NEW orders during STORM."""
    exempt_guard.update(latency_us=25000)
    assert exempt_guard.state == StormGuardState.STORM
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert ok
    assert reason == "STORM_EXEMPT"


def test_non_exempt_strategy_still_blocked_during_storm(exempt_guard):
    """Non-exempt strategy is still blocked during STORM."""
    exempt_guard.update(latency_us=25000)
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "cbs_tmfd6"))
    assert not ok
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_exempt_strategy_cancel_still_allowed_during_halt(exempt_guard):
    """CANCEL always allowed regardless of exemption."""
    exempt_guard.trigger_halt("test")
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.CANCEL, "spike_fader"))
    assert ok
    assert reason == "OK"


def test_exempt_strategy_normal_state_returns_ok(exempt_guard):
    """In NORMAL state, exempt strategies get OK (not HALT_EXEMPT)."""
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert ok
    assert reason == "OK"


def test_no_exempt_strategies_default():
    """Default StormGuard has no exempt strategies."""
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        g = StormGuard()
    assert g._halt_exempt_strategies == frozenset()


def test_env_var_halt_exempt_strategies():
    """HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES env var populates exempt set."""
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        with patch.dict("os.environ", {"HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES": "spike_fader,event_momentum"}):
            g = StormGuard()
    assert g._halt_exempt_strategies == frozenset({"spike_fader", "event_momentum"})


# ── is_halt_exempt public API ──────────────────────────────────────────────


def test_is_halt_exempt_returns_true_for_exempt(exempt_guard):
    """is_halt_exempt returns True for registered strategy."""
    assert exempt_guard.is_halt_exempt("spike_fader") is True


def test_is_halt_exempt_returns_false_for_non_exempt(exempt_guard):
    """is_halt_exempt returns False for unregistered strategy."""
    assert exempt_guard.is_halt_exempt("cbs_tmfd6") is False


def test_is_halt_exempt_empty_guard(guard):
    """is_halt_exempt returns False when no strategies are exempt."""
    assert guard.is_halt_exempt("any_strategy") is False


# ── revoke_halt_exemption runtime kill switch ──────────────────────────────


def test_revoke_halt_exemption_removes_strategy(exempt_guard):
    """Revoking an exempt strategy removes it from the set."""
    assert exempt_guard.is_halt_exempt("spike_fader") is True
    result = exempt_guard.revoke_halt_exemption("spike_fader")
    assert result is True
    assert exempt_guard.is_halt_exempt("spike_fader") is False


def test_revoke_halt_exemption_returns_false_for_unknown(exempt_guard):
    """Revoking a non-exempt strategy returns False."""
    result = exempt_guard.revoke_halt_exemption("unknown_strategy")
    assert result is False


def test_revoke_halt_exemption_blocks_previously_exempt_orders(exempt_guard):
    """After revocation, previously exempt strategy is blocked during HALT."""
    exempt_guard.trigger_halt("test")
    # Before revocation: exempt
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert ok
    assert reason == "HALT_EXEMPT"

    # Revoke
    exempt_guard.revoke_halt_exemption("spike_fader")

    # After revocation: blocked
    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert not ok
    assert reason == "STORMGUARD_HALT"


# ── grant_halt_exemption ──────────────────────────────────────────────────


def test_grant_halt_exemption_adds_strategy(guard):
    """Granting exemption allows strategy through HALT."""
    guard.trigger_halt("test")
    # Before grant: blocked
    ok, _ = guard.validate(_make_intent_with_strategy(IntentType.NEW, "new_mm"))
    assert not ok

    # Grant
    result = guard.grant_halt_exemption("new_mm")
    assert result is True
    assert guard.is_halt_exempt("new_mm") is True

    # After grant: allowed
    ok, reason = guard.validate(_make_intent_with_strategy(IntentType.NEW, "new_mm"))
    assert ok
    assert reason == "HALT_EXEMPT"


def test_grant_halt_exemption_idempotent(guard):
    """Granting exemption twice is safe."""
    guard.grant_halt_exemption("mm_strategy")
    guard.grant_halt_exemption("mm_strategy")
    assert guard.is_halt_exempt("mm_strategy") is True


# ── stormguard_halt_exempt_bypass_total counter ───────────────────────────


def test_halt_exempt_bypass_increments_dedicated_counter(exempt_guard):
    """Halt-exempt bypass increments stormguard_halt_exempt_bypass_total, NOT
    stormguard_transitions_total, ensuring bypass events are tracked separately
    from FSM state transitions."""
    exempt_guard.trigger_halt("drift_burst")

    ok, reason = exempt_guard.validate(_make_intent_with_strategy(IntentType.NEW, "spike_fader"))
    assert ok
    assert reason == "HALT_EXEMPT"

    metrics = exempt_guard.metrics
    # Dedicated bypass counter must be incremented
    metrics.stormguard_halt_exempt_bypass_total.inc.assert_called()
    # Transition counter must NOT be called with "halt_exempt_bypass"
    for call in metrics.stormguard_transitions_total.labels.call_args_list:
        assert call.kwargs.get("direction") != "halt_exempt_bypass", (
            "stormguard_transitions_total must not use direction='halt_exempt_bypass'"
        )


# ── report_feature_recovery does NOT transition (Fix 4) ───────────────────


def test_feature_recovery_does_not_transition_when_other_storm_active(guard):
    """Feature recovery only clears flag; does not de-escalate if other
    STORM conditions (latency) are still active."""
    # Latency causes STORM
    guard.update(latency_us=25000)
    assert guard.state == StormGuardState.STORM

    # Feature failure also fires (state already STORM)
    guard.report_feature_failure(count=10)
    assert guard._feature_failure_active is True

    # Bypass anti-flap hold
    guard._feature_failure_storm_ts -= 10.0

    # Feature recovers — but latency is still elevated
    guard.report_feature_recovery()
    assert guard._feature_failure_active is False
    # State MUST remain STORM (latency still elevated)
    assert guard.state == StormGuardState.STORM


def test_feature_recovery_clears_flag_only(guard):
    """Feature recovery only clears the flag; next update() handles state."""
    guard.report_feature_failure(count=10)
    assert guard.state == StormGuardState.STORM
    guard._feature_failure_storm_ts -= 10.0

    guard.report_feature_recovery()
    assert guard._feature_failure_active is False
    # State is still STORM until update() clears it
    assert guard.state == StormGuardState.STORM

    # Now update() with clear inputs de-escalates via hysteresis
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 1
    guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0)
    assert guard.state == StormGuardState.NORMAL


# ── _feature_failure_active blocks de-escalation ───────────────────────────


def test_feature_failure_prevents_normal_deescalation(guard):
    """_evaluate_target_state returns STORM when _feature_failure_active is True."""
    guard._feature_failure_active = True
    state, reason = guard._evaluate_target_state(0, 0, 0.0)
    assert state == StormGuardState.STORM
    assert reason == "FeatureEngine failure active"


def test_feature_failure_cleared_allows_normal(guard):
    """_evaluate_target_state returns NORMAL once _feature_failure_active is cleared."""
    guard._feature_failure_active = True
    guard._feature_failure_active = False
    state, reason = guard._evaluate_target_state(0, 0, 0.0)
    assert state == StormGuardState.NORMAL
    assert reason == ""


def test_feature_failure_does_not_override_halt(guard):
    """HALT priority is preserved over _feature_failure_active (HALT > STORM)."""
    guard._feature_failure_active = True
    # halt_drawdown_bps default is -500; use a value that triggers HALT
    halt_drawdown = guard.thresholds.halt_drawdown_bps - 1
    state, _ = guard._evaluate_target_state(halt_drawdown, 0, 0.0)
    assert state == StormGuardState.HALT


# ── Feed gap re-escalation cooldown ──────────────────────────────────────────


def test_feed_gap_reescalation_suppressed_after_deescalation(guard):
    """After de-escalating from a feed-gap STORM, re-escalation via feed gap
    should be suppressed for a cooldown period to prevent flapping.

    Production observation: 75 STORM transitions in 6 hours from feed_gap
    oscillating around the threshold.
    """
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 1
    guard._feed_gap_reescalation_cooldown_s = 120.0

    # Enter STORM via feed gap
    state = guard.update(feed_gap_s=2.0)
    assert state == StormGuardState.STORM

    # De-escalate
    state = guard.update(feed_gap_s=0.0)
    assert state == StormGuardState.NORMAL

    # Immediately re-trigger feed gap — should be suppressed
    state = guard.update(feed_gap_s=2.0)
    assert state == StormGuardState.NORMAL  # suppressed, NOT STORM


def test_feed_gap_reescalation_allowed_after_cooldown(guard):
    """Feed gap re-escalation is allowed once the cooldown period expires."""
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 1
    guard._feed_gap_reescalation_cooldown_s = 0.0  # no cooldown

    # Enter STORM, de-escalate, re-trigger
    guard.update(feed_gap_s=2.0)
    guard.update(feed_gap_s=0.0)
    assert guard.state == StormGuardState.NORMAL

    state = guard.update(feed_gap_s=2.0)
    assert state == StormGuardState.STORM  # allowed, cooldown is 0


def test_non_feed_gap_escalation_not_suppressed(guard):
    """Latency/drawdown STORM is never suppressed by feed gap cooldown."""
    guard._storm_cooldown_s = 0.0
    guard._de_escalate_threshold = 1
    guard._feed_gap_reescalation_cooldown_s = 9999.0

    # Enter STORM via feed gap, de-escalate
    guard.update(feed_gap_s=2.0)
    guard.update(feed_gap_s=0.0)
    assert guard.state == StormGuardState.NORMAL

    # Latency-triggered STORM should NOT be suppressed
    state = guard.update(latency_us=25000)
    assert state == StormGuardState.STORM

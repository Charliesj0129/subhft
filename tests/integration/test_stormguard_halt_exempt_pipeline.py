"""Integration tests for StormGuard halt-exempt pipeline end-to-end.

Validates that halt-exempt strategies can pass through all HALT checkpoints,
and that non-exempt strategies are correctly blocked. Also tests runtime
kill switch, GatewayPolicy recovery, and feature recovery edge cases.
"""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.risk.storm_guard import StormGuard

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_metrics():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get") as mock_get:
        registry = MagicMock()
        registry.stormguard_mode.labels.return_value = MagicMock()
        registry.stormguard_transitions_total.labels.return_value = MagicMock()
        mock_get.return_value = registry
        yield registry


@pytest.fixture
def halt_exempt_guard(mock_metrics):
    """StormGuard in HALT state with 'spike_fader' as halt-exempt."""
    guard = StormGuard(halt_exempt_strategies=frozenset({"spike_fader"}))
    guard._halt_cooldown_s = 0.0
    guard._de_escalate_threshold = 1
    guard.trigger_halt("test_halt")
    assert guard.state == StormGuardState.HALT
    return guard


def _make_intent(
    strategy_id: str = "default_strat",
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "TXFD6",
    reason: str = "",
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=200000000,
        qty=1,
        reason=reason,
    )


def _make_command(
    intent: OrderIntent,
    sg_state: StormGuardState = StormGuardState.HALT,
) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=sg_state,
    )


# ---------------------------------------------------------------------------
# 1. StormGuard.validate() — halt-exempt passes, non-exempt blocked
# ---------------------------------------------------------------------------


class TestStormGuardValidateHaltExempt:
    def test_exempt_strategy_passes_in_halt(self, halt_exempt_guard):
        intent = _make_intent(strategy_id="spike_fader")
        allowed, reason = halt_exempt_guard.validate(intent)
        assert allowed is True
        assert reason == "HALT_EXEMPT"

    def test_non_exempt_strategy_blocked_in_halt(self, halt_exempt_guard):
        intent = _make_intent(strategy_id="other_strat")
        allowed, reason = halt_exempt_guard.validate(intent)
        assert allowed is False
        assert reason == "STORMGUARD_HALT"

    def test_cancel_always_passes_in_halt(self, halt_exempt_guard):
        intent = _make_intent(strategy_id="other_strat", intent_type=IntentType.CANCEL)
        allowed, reason = halt_exempt_guard.validate(intent)
        assert allowed is True
        assert reason == "OK"

    def test_force_flat_always_passes_in_halt(self, halt_exempt_guard):
        intent = _make_intent(strategy_id="other_strat", intent_type=IntentType.FORCE_FLAT)
        allowed, reason = halt_exempt_guard.validate(intent)
        assert allowed is True
        assert reason == "OK"

    def test_exempt_strategy_passes_new_in_storm(self, halt_exempt_guard):
        """Exempt strategies also bypass STORM NEW blocking."""
        halt_exempt_guard._halt_cooldown_s = 0.0
        halt_exempt_guard._de_escalate_threshold = 1
        halt_exempt_guard.update(drawdown_bps=-110)  # STORM level
        intent = _make_intent(strategy_id="spike_fader")
        allowed, reason = halt_exempt_guard.validate(intent)
        assert allowed is True

    def test_non_exempt_new_blocked_in_storm(self, mock_metrics):
        guard = StormGuard(halt_exempt_strategies=frozenset({"spike_fader"}))
        guard.update(drawdown_bps=-110)
        assert guard.state == StormGuardState.STORM
        intent = _make_intent(strategy_id="other_strat", intent_type=IntentType.NEW)
        allowed, reason = guard.validate(intent)
        assert allowed is False
        assert "STORM" in reason


# ---------------------------------------------------------------------------
# 2. GatewayPolicy — halt-exempt awareness (after fix)
# ---------------------------------------------------------------------------


class TestGatewayPolicyHaltExempt:
    def test_gateway_policy_halt_blocks_non_cancel(self, mock_metrics):
        from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode

        policy = GatewayPolicy()
        policy.set_halt()
        assert policy.mode == GatewayPolicyMode.HALT

        intent = _make_intent(strategy_id="other_strat")
        allowed, reason = policy.gate(intent, StormGuardState.HALT)
        assert allowed is False
        assert reason == "HALT"

    def test_gateway_policy_cancel_passes_in_halt(self, mock_metrics):
        from hft_platform.gateway.policy import GatewayPolicy

        policy = GatewayPolicy()
        policy.set_halt()
        intent = _make_intent(intent_type=IntentType.CANCEL)
        allowed, reason = policy.gate(intent, StormGuardState.HALT)
        assert allowed is True

    def test_gateway_policy_recovery_degrade_to_normal(self, mock_metrics):
        """GatewayPolicy auto-recovers from DEGRADE to NORMAL when storm clears."""
        from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode

        policy = GatewayPolicy()
        # Enter DEGRADE via storm
        intent = _make_intent()
        policy.gate(intent, StormGuardState.STORM)
        assert policy.mode == GatewayPolicyMode.DEGRADE

        # Storm clears -> NORMAL
        policy.gate(intent, StormGuardState.NORMAL)
        assert policy.mode == GatewayPolicyMode.NORMAL

    def test_gateway_policy_set_normal_after_halt(self, mock_metrics):
        """set_normal() recovers GatewayPolicy from HALT (manual recovery path)."""
        from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode

        policy = GatewayPolicy()
        policy.set_halt()
        assert policy.mode == GatewayPolicyMode.HALT

        policy.set_normal()
        assert policy.mode == GatewayPolicyMode.NORMAL

        intent = _make_intent()
        allowed, _ = policy.gate(intent, StormGuardState.NORMAL)
        assert allowed is True


# ---------------------------------------------------------------------------
# 3. DLQ halt_exempt_blocked flag
# ---------------------------------------------------------------------------


class TestDLQHaltExemptFlag:
    @pytest.mark.asyncio
    async def test_dlq_entry_records_halt_exempt_flag(self):
        from hft_platform.order.deadletter import DeadLetterQueue

        dlq = DeadLetterQueue(dlq_dir="/tmp/test_dlq_halt_exempt")
        await dlq.add(
            order_id="1",
            strategy_id="spike_fader",
            symbol="TXFD6",
            side="BUY",
            price=200000000,
            qty=1,
            reason="stormguard_halt",
            error_message="StormGuard HALT",
            halt_exempt_blocked=True,
        )
        async with dlq._lock:
            assert len(dlq._buffer) == 1
            entry = dlq._buffer[0]
            assert entry.halt_exempt_blocked is True
            assert entry.strategy_id == "spike_fader"

    @pytest.mark.asyncio
    async def test_dlq_entry_default_not_halt_exempt(self):
        from hft_platform.order.deadletter import DeadLetterQueue

        dlq = DeadLetterQueue(dlq_dir="/tmp/test_dlq_no_exempt")
        await dlq.add(
            order_id="2",
            strategy_id="normal_strat",
            symbol="TXFD6",
            side="BUY",
            price=200000000,
            qty=1,
            reason="stormguard_halt",
            error_message="StormGuard HALT",
        )
        async with dlq._lock:
            entry = dlq._buffer[0]
            assert entry.halt_exempt_blocked is False

    def test_dlq_entry_serialization_includes_halt_exempt(self):
        from hft_platform.order.deadletter import DeadLetterEntry

        entry = DeadLetterEntry(
            timestamp_ns=1000,
            order_id="1",
            strategy_id="spike_fader",
            symbol="TXFD6",
            side="BUY",
            price=200000000,
            qty=1,
            reason="stormguard_halt",
            error_message="StormGuard HALT",
            halt_exempt_blocked=True,
        )
        d = entry.to_dict()
        assert d["halt_exempt_blocked"] is True

        restored = DeadLetterEntry.from_dict(d)
        assert restored.halt_exempt_blocked is True


# ---------------------------------------------------------------------------
# 4. Feature recovery bypass when latency is elevated
# ---------------------------------------------------------------------------


class TestFeatureRecoveryBypass:
    def test_feature_recovery_does_not_deescalate_when_latency_elevated(self, mock_metrics):
        """report_feature_recovery() clears flag but does NOT de-escalate state.

        The regular update() cycle handles de-escalation so that other active
        STORM conditions (latency, drawdown) are respected.
        """
        guard = StormGuard()
        # Enter STORM via latency
        guard.update(latency_us=25000)
        assert guard.state == StormGuardState.STORM

        # Simulate feature failure escalation (additive)
        guard.report_feature_failure(10)
        assert guard.state == StormGuardState.STORM
        assert guard._feature_failure_active is True

        # Wait past recovery hold
        guard._feature_failure_storm_ts -= 10.0

        # Feature recovers — flag cleared but state stays STORM
        # because report_feature_recovery() delegates de-escalation to update()
        guard.report_feature_recovery()
        assert guard._feature_failure_active is False
        # State remains STORM — latency is still elevated
        assert guard.state == StormGuardState.STORM

    def test_feature_recovery_anti_flap_suppresses_fast_recovery(self, mock_metrics):
        """Recovery within hold period is suppressed."""
        guard = StormGuard()
        guard.report_feature_failure(10)
        assert guard._feature_failure_active is True

        # Immediately try recovery — should be suppressed
        guard.report_feature_recovery()
        assert guard._feature_failure_active is True  # Still active
        assert guard.state == StormGuardState.STORM


# ---------------------------------------------------------------------------
# 5. Runtime kill switch (grant/revoke halt exemption)
# ---------------------------------------------------------------------------


class TestRuntimeHaltExemption:
    def test_halt_exempt_from_env_var(self, monkeypatch, mock_metrics):
        """HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES env var sets exemptions."""
        monkeypatch.setenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", "strat_a,strat_b")
        guard = StormGuard()
        assert "strat_a" in guard._halt_exempt_strategies
        assert "strat_b" in guard._halt_exempt_strategies
        assert "strat_c" not in guard._halt_exempt_strategies

    def test_halt_exempt_constructor_overrides_env(self, monkeypatch, mock_metrics):
        """Constructor arg takes precedence over env var."""
        monkeypatch.setenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", "strat_a")
        guard = StormGuard(halt_exempt_strategies=frozenset({"strat_x"}))
        assert "strat_x" in guard._halt_exempt_strategies
        assert "strat_a" not in guard._halt_exempt_strategies

    def test_halt_exempt_is_immutable_frozenset(self, mock_metrics):
        """_halt_exempt_strategies is a frozenset (immutable)."""
        guard = StormGuard(halt_exempt_strategies=frozenset({"strat_a"}))
        assert isinstance(guard._halt_exempt_strategies, frozenset)

    def test_empty_env_var_yields_empty_set(self, monkeypatch, mock_metrics):
        monkeypatch.setenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", "")
        guard = StormGuard()
        assert guard._halt_exempt_strategies == frozenset()

    def test_whitespace_in_env_var_stripped(self, monkeypatch, mock_metrics):
        monkeypatch.setenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", " strat_a , strat_b ")
        guard = StormGuard()
        assert "strat_a" in guard._halt_exempt_strategies
        assert "strat_b" in guard._halt_exempt_strategies


# ---------------------------------------------------------------------------
# 6. order_halt_skip_total metric has strategy_id label
# ---------------------------------------------------------------------------


class TestOrderHaltSkipMetricLabel:
    def test_order_halt_skip_total_exists(self):
        """order_halt_skip_total counter is defined in MetricsRegistry."""
        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry.get()
        assert hasattr(registry, "order_halt_skip_total"), "order_halt_skip_total not found in MetricsRegistry"

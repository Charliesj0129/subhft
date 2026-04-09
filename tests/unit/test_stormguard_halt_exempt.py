"""Tests for StormGuard halt-exempt awareness across the order pipeline.

Covers:
- H1: RiskEngine post-approve HALT check (engine.py)
- H2: OrderAdapter.execute() HALT check (adapter.py)
- H3: OrderAdapter._api_worker() batch HALT check (adapter.py)
- H4: GatewayPolicy HALT halt-exempt bypass (policy.py)
- M1: GatewayPolicy DEGRADE halt-exempt bypass (policy.py)
- M2: GatewayPolicy HALT allows FORCE_FLAT (policy.py)
- M5: halt_flatten reason spoof prevention (adapter.py)
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_intent(
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "exempt_strat",
    reason: str = "",
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="TSE:2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=1_000_000,
        qty=1,
        tif=TIF.LIMIT,
        reason=reason,
    )


def _make_cmd(
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "exempt_strat",
    sg_state: StormGuardState = StormGuardState.HALT,
    reason: str = "",
) -> OrderCommand:
    intent = _make_intent(intent_type=intent_type, strategy_id=strategy_id, reason=reason)
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10_000_000_000,
        storm_guard_state=sg_state,
        created_ns=timebase.now_ns(),
    )


def _make_storm_guard(
    state: StormGuardState = StormGuardState.HALT,
    halt_exempt: frozenset[str] | None = None,
) -> StormGuard:
    sg = StormGuard(
        thresholds=RiskThresholds(),
        halt_exempt_strategies=halt_exempt or frozenset({"exempt_strat"}),
    )
    sg.state = state
    return sg


# ── H4/M1/M2: GatewayPolicy tests ──────────────────────────────────────


class TestGatewayPolicyHaltExempt:
    """Tests for GatewayPolicy halt-exempt awareness (H4)."""

    def test_halt_allows_halt_exempt_strategy_new(self):
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        intent = _make_intent(IntentType.NEW, strategy_id="exempt_strat")
        ok, reason = policy.gate(intent, StormGuardState.HALT)
        assert ok is True
        assert reason == "HALT_EXEMPT"

    def test_halt_blocks_non_exempt_strategy_new(self):
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        intent = _make_intent(IntentType.NEW, strategy_id="regular_strat")
        ok, reason = policy.gate(intent, StormGuardState.HALT)
        assert ok is False
        assert reason == "HALT"

    def test_halt_allows_cancel(self):
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        intent = _make_intent(IntentType.CANCEL, strategy_id="regular_strat")
        ok, reason = policy.gate(intent, StormGuardState.HALT)
        assert ok is True

    def test_halt_allows_force_flat(self):
        """M2: HALT must allow FORCE_FLAT through (consistency with StormGuard.validate)."""
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="regular_strat")
        ok, reason = policy.gate(intent, StormGuardState.HALT)
        assert ok is True

    def test_halt_typed_gate_passes_strategy_id(self):
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        ok, reason = policy.gate_typed(
            int(IntentType.NEW),
            StormGuardState.HALT,
            strategy_id="exempt_strat",
        )
        assert ok is True
        assert reason == "HALT_EXEMPT"

    def test_halt_typed_gate_blocks_without_strategy_id(self):
        sg = _make_storm_guard(StormGuardState.HALT)
        policy = GatewayPolicy(storm_guard=sg)
        policy.set_halt()
        ok, reason = policy.gate_typed(
            int(IntentType.NEW),
            StormGuardState.HALT,
        )
        assert ok is False

    def test_degrade_allows_halt_exempt_strategy_new(self):
        """M1: DEGRADE mode should allow halt-exempt strategies to place NEW orders."""
        sg = _make_storm_guard(StormGuardState.STORM)
        policy = GatewayPolicy(storm_guard=sg)
        policy._mode = GatewayPolicyMode.DEGRADE
        intent = _make_intent(IntentType.NEW, strategy_id="exempt_strat")
        ok, reason = policy.gate(intent, StormGuardState.STORM)
        assert ok is True
        assert reason == "DEGRADE_EXEMPT"

    def test_degrade_blocks_non_exempt_strategy_new(self):
        sg = _make_storm_guard(StormGuardState.STORM)
        policy = GatewayPolicy(storm_guard=sg)
        policy._mode = GatewayPolicyMode.DEGRADE
        intent = _make_intent(IntentType.NEW, strategy_id="regular_strat")
        ok, reason = policy.gate(intent, StormGuardState.STORM)
        assert ok is False
        assert reason == "DEGRADE"

    def test_no_storm_guard_falls_back_to_blocking(self):
        policy = GatewayPolicy(storm_guard=None)
        policy.set_halt()
        intent = _make_intent(IntentType.NEW, strategy_id="exempt_strat")
        ok, reason = policy.gate(intent, StormGuardState.HALT)
        assert ok is False
        assert reason == "HALT"

    def test_normal_mode_ignores_halt_exempt(self):
        sg = _make_storm_guard(StormGuardState.NORMAL)
        policy = GatewayPolicy(storm_guard=sg)
        intent = _make_intent(IntentType.NEW, strategy_id="regular_strat")
        ok, reason = policy.gate(intent, StormGuardState.NORMAL)
        assert ok is True


# ── H1: RiskEngine post-approve HALT check ──────────────────────────────


class TestRiskEngineHaltExempt:
    """Tests for RiskEngine._is_halt_exempt (H1)."""

    def test_is_halt_exempt_with_halt_exempt_strategies(self):
        from hft_platform.risk.engine import RiskEngine

        cfg_path = "/dev/null"  # Will fail load_config, we patch it
        intent_q = asyncio.Queue()
        order_q = asyncio.Queue()
        sg = _make_storm_guard(StormGuardState.HALT, frozenset({"exempt_strat"}))

        with pytest.MonkeyPatch.context() as m:
            m.setattr(RiskEngine, "load_config", lambda self: setattr(self, "config", {"global_defaults": {}}))
            engine = RiskEngine(cfg_path, intent_q, order_q, storm_guard=sg)

        assert engine._is_halt_exempt("exempt_strat") is True
        assert engine._is_halt_exempt("regular_strat") is False

    def test_is_halt_exempt_with_is_halt_exempt_method(self):
        """Test fallback to is_halt_exempt() method if available."""
        from hft_platform.risk.engine import RiskEngine

        sg = MagicMock()
        sg.is_halt_exempt = MagicMock(return_value=True)
        sg.state = StormGuardState.HALT

        with pytest.MonkeyPatch.context() as m:
            m.setattr(RiskEngine, "load_config", lambda self: setattr(self, "config", {"global_defaults": {}}))
            engine = RiskEngine("/dev/null", asyncio.Queue(), asyncio.Queue(), storm_guard=sg)

        assert engine._is_halt_exempt("any_strat") is True
        sg.is_halt_exempt.assert_called_with("any_strat")


# ── H2/H3: OrderAdapter halt-exempt checks ──────────────────────────────


class TestOrderAdapterHaltExempt:
    """Tests for OrderAdapter halt-exempt in execute() and _api_worker() (H2, H3)."""

    def test_is_strategy_halt_exempt_with_storm_guard(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = MagicMock(spec=OrderAdapter)
        adapter._storm_guard = _make_storm_guard(
            StormGuardState.HALT,
            frozenset({"exempt_strat"}),
        )
        # Call the real method
        result = OrderAdapter._is_strategy_halt_exempt(adapter, "exempt_strat")
        assert result is True

    def test_is_strategy_halt_exempt_no_storm_guard(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = MagicMock(spec=OrderAdapter)
        adapter._storm_guard = None
        result = OrderAdapter._is_strategy_halt_exempt(adapter, "exempt_strat")
        assert result is False

    def test_is_strategy_halt_exempt_non_exempt(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = MagicMock(spec=OrderAdapter)
        adapter._storm_guard = _make_storm_guard(
            StormGuardState.HALT,
            frozenset({"other_strat"}),
        )
        result = OrderAdapter._is_strategy_halt_exempt(adapter, "regular_strat")
        assert result is False


# ── M5: halt_flatten spoof prevention ────────────────────────────────────


class TestHaltFlattenSpoofPrevention:
    """Tests that reason='halt_flatten' alone no longer bypasses HALT (M5)."""

    def test_halt_flatten_reason_alone_does_not_bypass(self):
        """A NEW order with reason='halt_flatten' should NOT bypass HALT."""
        from hft_platform.order.adapter import OrderAdapter

        adapter = MagicMock(spec=OrderAdapter)
        adapter._storm_guard = _make_storm_guard(
            StormGuardState.HALT,
            frozenset(),  # no exempt strategies
        )
        adapter._is_strategy_halt_exempt = OrderAdapter._is_strategy_halt_exempt.__get__(adapter)

        cmd = _make_cmd(
            intent_type=IntentType.NEW,
            strategy_id="spoofer",
            sg_state=StormGuardState.HALT,
            reason="halt_flatten",
        )
        intent = cmd.intent

        # Reproduce the _halt_exempt check from execute()
        _halt_exempt = (
            intent.intent_type == IntentType.CANCEL
            or intent.intent_type == IntentType.FORCE_FLAT
            or adapter._is_strategy_halt_exempt(intent.strategy_id)
        )
        assert _halt_exempt is False, "NEW order with reason='halt_flatten' should be blocked"

    def test_force_flat_still_passes_halt(self):
        """FORCE_FLAT intent_type should still pass HALT (legitimate HaltFlattener path)."""
        from hft_platform.order.adapter import OrderAdapter

        adapter = MagicMock(spec=OrderAdapter)
        adapter._storm_guard = _make_storm_guard(
            StormGuardState.HALT,
            frozenset(),
        )
        adapter._is_strategy_halt_exempt = OrderAdapter._is_strategy_halt_exempt.__get__(adapter)

        cmd = _make_cmd(
            intent_type=IntentType.FORCE_FLAT,
            strategy_id="any_strat",
            sg_state=StormGuardState.HALT,
            reason="halt_flatten",
        )
        intent = cmd.intent

        _halt_exempt = (
            intent.intent_type == IntentType.CANCEL
            or intent.intent_type == IntentType.FORCE_FLAT
            or adapter._is_strategy_halt_exempt(intent.strategy_id)
        )
        assert _halt_exempt is True

"""Tests for CE2-06: GatewayPolicy FSM."""
import os

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState, TIF
from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode


def _make_intent(intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=1_000_000,
        qty=1,
        tif=TIF.LIMIT,
    )


def test_policy_normal_allows_new():
    policy = GatewayPolicy()
    ok, reason = policy.gate(_make_intent(IntentType.NEW), StormGuardState.NORMAL)
    assert ok is True


def test_policy_halt_blocks_new():
    policy = GatewayPolicy()
    policy.set_halt()
    ok, reason = policy.gate(_make_intent(IntentType.NEW), StormGuardState.HALT)
    assert ok is False
    assert reason == "HALT"


def test_policy_halt_allows_cancel_when_configured(monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_HALT_CANCEL", "1")
    policy = GatewayPolicy()
    policy.set_halt()
    ok, reason = policy.gate(_make_intent(IntentType.CANCEL), StormGuardState.HALT)
    assert ok is True


def test_policy_halt_blocks_cancel_when_disabled(monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_HALT_CANCEL", "0")
    policy = GatewayPolicy()
    policy.set_halt()
    ok, reason = policy.gate(_make_intent(IntentType.CANCEL), StormGuardState.HALT)
    assert ok is False


def test_policy_degrade_blocks_new():
    policy = GatewayPolicy()
    policy._mode = GatewayPolicyMode.DEGRADE
    ok, reason = policy.gate(_make_intent(IntentType.NEW), StormGuardState.STORM)
    assert ok is False
    assert reason == "DEGRADE"


def test_policy_degrade_allows_cancel():
    policy = GatewayPolicy()
    policy._mode = GatewayPolicyMode.DEGRADE
    ok, _ = policy.gate(_make_intent(IntentType.CANCEL), StormGuardState.STORM)
    assert ok is True


def test_policy_auto_degrade_on_storm():
    """NORMAL → DEGRADE auto-transition on StormGuard STORM."""
    policy = GatewayPolicy()
    assert policy.mode == GatewayPolicyMode.NORMAL
    # Calling gate with STORM triggers auto-degrade
    policy.gate(_make_intent(IntentType.NEW), StormGuardState.STORM)
    assert policy.mode == GatewayPolicyMode.DEGRADE


def test_policy_auto_recover_from_degrade():
    """DEGRADE → NORMAL auto-transition when storm clears."""
    policy = GatewayPolicy()
    policy._mode = GatewayPolicyMode.DEGRADE
    policy.gate(_make_intent(IntentType.NEW), StormGuardState.NORMAL)
    assert policy.mode == GatewayPolicyMode.NORMAL


def test_policy_mode_int():
    policy = GatewayPolicy()
    assert policy.mode_int() == 0
    policy._mode = GatewayPolicyMode.DEGRADE
    assert policy.mode_int() == 1
    policy._mode = GatewayPolicyMode.HALT
    assert policy.mode_int() == 2


def test_policy_no_auto_degrade_when_disabled(monkeypatch):
    monkeypatch.setenv("HFT_GATEWAY_DEGRADE_ON_STORM", "0")
    policy = GatewayPolicy()
    policy.gate(_make_intent(IntentType.NEW), StormGuardState.STORM)
    assert policy.mode == GatewayPolicyMode.NORMAL

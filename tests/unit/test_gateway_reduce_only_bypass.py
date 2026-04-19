"""Regression tests for Bug 22 (2026-04-17 R47 TMFE6 Gateway HALT deadlock).

Symptom (extended from Bug 21): after Bug 21 fix was deployed, R47 still got
rejected when trying to cover a short during StormGuard HALT because the
GatewayPolicy (which runs BEFORE StormGuard in the pipeline) has its own
``_gate_by_intent_type`` that only allows CANCEL/FORCE_FLAT through HALT.
Cover BUY intents (IntentType.NEW, side=BUY) against a short position hit
``return False, "HALT"`` at ``gateway/policy.py:115``.

Log evidence (21:27:41 CST):
  {"symbol": "TMFE6", "reason": "HALT", "side": "0",
   "event": "r47_risk_rejection_pending_released"}

Fix: Add ``_reduces_position(symbol, strategy_id, side, qty)`` helper wired via
``set_position_provider()``. Allow HALT/DEGRADE cover orders to bypass with
new reason codes ``HALT_REDUCE_ONLY`` / ``DEGRADE_REDUCE_ONLY``.
"""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.gateway.policy import GatewayPolicy


def _position_provider(positions: dict[tuple[str, str], int]):
    def _fn(symbol: str, strategy_id: str) -> int:
        return positions.get((symbol, strategy_id), 0)

    return _fn


def _make_intent(
    intent_type=IntentType.NEW,
    side=Side.BUY,
    qty=1,
    symbol="TMFE6",
    strategy_id="R47",
):
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=1_000_000,
        qty=qty,
    )


class TestGatewayHaltReduceOnly:
    def _make_policy(self, position: int = 0) -> GatewayPolicy:
        policy = GatewayPolicy()
        # bypass startup holdoff
        policy._startup_holdoff_until = 0.0
        policy.set_halt()
        policy.set_position_provider(_position_provider({("TMFE6", "R47"): position}))
        return policy

    def test_halt_blocks_opener_without_position(self):
        policy = self._make_policy(position=0)
        ok, reason = policy.gate(_make_intent(side=Side.BUY, qty=1), StormGuardState.HALT)
        assert not ok
        assert reason == "HALT"

    def test_halt_allows_cover_short(self):
        """Bug 22: cover BUY for short -1 must pass through HALT."""
        policy = self._make_policy(position=-1)
        ok, reason = policy.gate(_make_intent(side=Side.BUY, qty=1), StormGuardState.HALT)
        assert ok, f"Cover under HALT must bypass, got {reason}"
        assert reason == "HALT_REDUCE_ONLY"

    def test_halt_allows_cover_long(self):
        policy = self._make_policy(position=+1)
        ok, reason = policy.gate(_make_intent(side=Side.SELL, qty=1), StormGuardState.HALT)
        assert ok, f"Long exit under HALT must bypass, got {reason}"
        assert reason == "HALT_REDUCE_ONLY"

    def test_halt_blocks_overshoot_flip(self):
        """Short -1, BUY 2 → new=+1, |+1|=|-1| not reducing → still blocked."""
        policy = self._make_policy(position=-1)
        ok, _ = policy.gate(_make_intent(side=Side.BUY, qty=2), StormGuardState.HALT)
        assert not ok

    def test_halt_blocks_add_to_short(self):
        """Short -1, SELL 1 (grow short) → not reducing → blocked."""
        policy = self._make_policy(position=-1)
        ok, _ = policy.gate(_make_intent(side=Side.SELL, qty=1), StormGuardState.HALT)
        assert not ok

    def test_cancel_still_passes_in_halt(self):
        policy = self._make_policy(position=0)
        ok, _ = policy.gate(_make_intent(intent_type=IntentType.CANCEL), StormGuardState.HALT)
        assert ok

    def test_force_flat_still_passes_in_halt(self):
        policy = self._make_policy(position=-1)
        ok, _ = policy.gate(_make_intent(intent_type=IntentType.FORCE_FLAT), StormGuardState.HALT)
        assert ok

    def test_no_position_provider_reverts_to_halt_block(self):
        """Without wired provider, cannot classify — conservative default (block)."""
        policy = GatewayPolicy()
        policy._startup_holdoff_until = 0.0
        policy.set_halt()
        ok, reason = policy.gate(_make_intent(side=Side.BUY, qty=1), StormGuardState.HALT)
        assert not ok
        assert reason == "HALT"


class TestGatewayDegradeReduceOnly:
    def _make_policy(self, position: int = 0) -> GatewayPolicy:
        # DEGRADE is entered automatically on STORM; simulate via set and STORM state
        policy = GatewayPolicy()
        policy._startup_holdoff_until = 0.0
        from hft_platform.gateway.policy import GatewayPolicyMode

        policy._set_mode(GatewayPolicyMode.DEGRADE)
        policy.set_position_provider(_position_provider({("TMFE6", "R47"): position}))
        return policy

    def test_degrade_blocks_opener(self):
        policy = self._make_policy(position=0)
        # Call with STORM state to prevent auto-NORMAL transition
        ok, reason = policy.gate(_make_intent(side=Side.BUY, qty=1), StormGuardState.STORM)
        assert not ok
        assert reason == "DEGRADE"

    def test_degrade_allows_cover(self):
        policy = self._make_policy(position=-1)
        ok, reason = policy.gate(_make_intent(side=Side.BUY, qty=1), StormGuardState.STORM)
        assert ok, f"Cover under DEGRADE must bypass, got {reason}"
        assert reason == "DEGRADE_REDUCE_ONLY"


class TestGatewayTypedFastPath:
    """Typed fast path must also respect reduce-only bypass when symbol+side+qty supplied."""

    def _make_policy(self, position: int = 0) -> GatewayPolicy:
        policy = GatewayPolicy()
        policy._startup_holdoff_until = 0.0
        policy.set_halt()
        policy.set_position_provider(_position_provider({("TMFE6", "R47"): position}))
        return policy

    def test_typed_halt_allows_cover(self):
        policy = self._make_policy(position=-1)
        ok, reason = policy.gate_typed(
            intent_type=int(IntentType.NEW),
            sg_state=StormGuardState.HALT,
            strategy_id="R47",
            symbol="TMFE6",
            side=int(Side.BUY),
            qty=1,
        )
        assert ok
        assert reason == "HALT_REDUCE_ONLY"

    def test_typed_halt_backward_compat_without_extras(self):
        """Old 3-arg callers must still work (no bypass, behave as before)."""
        policy = self._make_policy(position=-1)
        ok, reason = policy.gate_typed(
            intent_type=int(IntentType.NEW),
            sg_state=StormGuardState.HALT,
            strategy_id="R47",
        )
        # Without symbol/side/qty, can't classify → falls back to HALT block
        assert not ok
        assert reason == "HALT"

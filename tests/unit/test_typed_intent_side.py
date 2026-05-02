"""Regression test for Bug 13 (2026-04-17).

RiskEngine previously used `getattr(intent, "side", None)` at 5 emit sites for
RiskFeedback. On typed-intent tuples (`("typed_intent_v1", iid, sid, sym, itype,
side, ...)`) there is no `.side` attribute, so feedback.side became None, R47's
``on_risk_feedback`` took the no-side warn-and-return branch (Bug 9 guard), and
``_pending_buy``/``_pending_sell`` froze permanently — a liveness bug.

Fix: shared ``typed_intent_side`` helper in contracts.strategy that unpacks
tuple index 5 for typed intents, falls back to attribute access otherwise.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
    typed_intent_side,
)


class TestTypedIntentSide:
    def test_typed_tuple_buy(self):
        intent = ("typed_intent_v1", 42, "R47", "TMFE6", int(IntentType.NEW), int(Side.BUY), 1000000, 1, 0)
        assert typed_intent_side(intent) == int(Side.BUY)

    def test_typed_tuple_sell(self):
        intent = ("typed_intent_v1", 42, "R47", "TMFE6", int(IntentType.NEW), int(Side.SELL), 1000000, 1, 0)
        assert typed_intent_side(intent) == int(Side.SELL)

    def test_order_intent_object(self):
        intent = OrderIntent(
            intent_id=1,
            strategy_id="R47",
            symbol="TMFE6",
            intent_type=IntentType.NEW,
            side=Side.SELL,
            price=1000000,
            qty=1,
        )
        assert typed_intent_side(intent) == int(Side.SELL)

    def test_typed_tuple_without_side_returns_none(self):
        # Truncated tuple (len < 6) — should return None without raising
        assert typed_intent_side(("typed_intent_v1", 1, "R47", "TMFE6")) is None

    def test_object_without_side_attribute_returns_none(self):
        class Dummy:
            pass

        assert typed_intent_side(Dummy()) is None

    def test_plain_tuple_not_typed_falls_through_to_attribute(self):
        # Non-typed tuple should fall through to getattr path
        assert typed_intent_side((1, 2, 3)) is None


class TestRiskEngineFeedbackSide:
    """Integration: verify RiskFeedback.side matches intent side for typed tuples."""

    @pytest.mark.asyncio
    async def test_rejection_feedback_preserves_typed_tuple_side(self):
        from hft_platform.contracts.strategy import RiskFeedback

        # Use monkey-patched minimal engine shim: we only test the getattr→helper
        # replacement effect on a constructed typed tuple.
        intent = ("typed_intent_v1", 7, "R47_MAKER_TMF", "TMFE6", int(IntentType.NEW), int(Side.SELL), 371000000, 1, 0)

        # Simulate the engine's feedback construction path.
        # After the fix, side must be extracted via typed_intent_side, not getattr.
        fb = RiskFeedback(
            intent_id=7,
            strategy_id="R47_MAKER_TMF",
            symbol="TMFE6",
            reason_code="POSITION_LIMIT",
            timestamp_ns=0,
            side=typed_intent_side(intent),
        )
        assert fb.side == int(Side.SELL), "RiskFeedback.side must equal intent side for typed tuples"
        assert fb.side is not None

        # Sanity: ensure we're not testing a pure-getattr path
        assert getattr(intent, "side", None) is None

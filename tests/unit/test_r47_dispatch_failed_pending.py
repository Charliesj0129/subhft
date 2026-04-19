"""Regression test for Bug 23 (2026-04-17 R47 max_pos=1 breach via phantom fills).

Symptom: R47 local_pos went to -2 briefly at 21:26:57-58 CST, violating
``max_pos=1`` hard cap. Broker actually received and filled TWO SELL orders
even though R47 only "intended" one per max_pos rules.

Log evidence:
  21:26:57.480 cmd_id=2 dispatch_failed → r47_risk_rejection_pending_released side=SELL
  21:26:57.545 cmd_id=4 dispatch_failed → r47_risk_rejection_pending_released side=SELL
  21:26:57.988 r47_fill SELL qty=1 price=37725 local_pos=-1
  21:26:58.953 r47_fill SELL qty=1 price=37730 local_pos=-2  ← BREACH

Root cause: ``OrderAdapter._api_worker`` catches dispatch exceptions and
marks the intent as ``phantom_order_candidate_dispatch_failed`` (expecting a
possible fill to arrive). BUT it ALSO calls ``_send_dispatch_rejection``
which emits ``RiskFeedback(reason_code="dispatch_failed")``. R47's
``on_risk_feedback`` decrements ``_pending_sell`` on that rejection. Then the
strategy's next tick sees ``pending_sell=0`` and passes ``can_sell`` → emits
another SELL → broker receives BOTH → double fill → max_pos breach.

Fix: For NEW / FORCE_FLAT phantom candidates, adapter flags the feedback with
``was_approved=True`` so R47's existing DEC2-001 guard (line 639 in
r47_maker.py) keeps the pending counter elevated. When the phantom fill
eventually arrives, ``on_fill`` decrements correctly. If no fill arrives,
pending stays elevated (safe liveness loss > unsafe max_pos breach).
"""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType, OrderIntent, RiskFeedback, Side


def _make_risk_feedback(side, reason_code, was_approved: bool = False) -> RiskFeedback:
    return RiskFeedback(
        intent_id=1,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFE6",
        reason_code=reason_code,
        timestamp_ns=0,
        side=side,
        was_approved=was_approved,
    )


def _make_r47():
    # Build R47 strategy instance with minimal init
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy.__new__(R47MakerStrategy)
    # Initialize all dicts/sets that on_risk_feedback may touch
    strat._local_pos = {}
    strat._pending_buy = {"TMFE6": 0}
    strat._pending_sell = {"TMFE6": 0}
    strat._active_buy_oid = {}
    strat._active_sell_oid = {}
    strat._last_bid = {}
    strat._last_ask = {}
    strat._last_quote_ns = {}
    strat._seen_fill_ids = set()
    strat._FILL_DEDUP_MAX = 500
    return strat


class TestDispatchFailedPreservesPending:
    """Bug 23: dispatch_failed with was_approved=True must NOT decrement pending."""

    def test_approved_flag_skips_decrement(self):
        """Baseline: DEC2-001 already handles was_approved=True."""
        strat = _make_r47()
        strat._pending_sell["TMFE6"] = 1

        fb = _make_risk_feedback(Side.SELL, "dispatch_failed", was_approved=True)
        strat.on_risk_feedback(fb)

        assert strat._pending_sell["TMFE6"] == 1, "was_approved=True should keep pending elevated"

    def test_dispatch_failed_without_approved_flag_decrements_todo(self):
        """Current state: without was_approved flag, pending DOES decrement.

        This is the R47 side of the bug. Adapter must set was_approved=True
        for phantom candidates so the strategy treats them as "approved but
        dispatch ambiguous" and preserves pending.
        """
        strat = _make_r47()
        strat._pending_sell["TMFE6"] = 1

        # CURRENT behavior (without fix): feedback.was_approved=False → decrement
        fb = _make_risk_feedback(Side.SELL, "dispatch_failed", was_approved=False)
        strat.on_risk_feedback(fb)
        assert strat._pending_sell["TMFE6"] == 0, (
            "Without was_approved=True, pending IS decremented (symptom reproduction)"
        )


class TestAdapterDispatchFailedFlagsApproved:
    """Adapter-side fix: phantom candidates receive was_approved=True feedback."""

    def test_send_dispatch_rejection_sets_approved_for_phantom(self):
        """Bug 23 fix: _send_dispatch_rejection uses was_approved=True
        when ``phantom_pending=True`` (signals ambiguous dispatch)."""
        from unittest.mock import MagicMock

        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter.__new__(OrderAdapter)
        mock_sink = MagicMock()
        mock_sink.put_nowait = MagicMock()
        adapter._rejection_sink = mock_sink
        adapter.metrics = MagicMock()

        intent = OrderIntent(
            intent_id=7,
            strategy_id="R47_MAKER_TMF",
            symbol="TMFE6",
            intent_type=IntentType.NEW,
            side=Side.SELL,
            price=37_730_0000,
            qty=1,
        )

        adapter._send_dispatch_rejection(intent, "dispatch_failed", phantom_pending=True)

        assert mock_sink.put_nowait.called
        feedback = mock_sink.put_nowait.call_args[0][0]
        assert isinstance(feedback, RiskFeedback)
        assert feedback.reason_code == "dispatch_failed"
        assert feedback.was_approved is True, "phantom_pending=True must flip was_approved=True to keep pending safe"

    def test_send_dispatch_rejection_default_is_not_approved(self):
        """Backward compat: default path keeps was_approved=False (existing behavior)."""
        from unittest.mock import MagicMock

        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter.__new__(OrderAdapter)
        mock_sink = MagicMock()
        mock_sink.put_nowait = MagicMock()
        adapter._rejection_sink = mock_sink
        adapter.metrics = MagicMock()

        intent = OrderIntent(
            intent_id=8,
            strategy_id="R47_MAKER_TMF",
            symbol="TMFE6",
            intent_type=IntentType.CANCEL,
            side=Side.SELL,
            price=0,
            qty=0,
        )

        # Without phantom_pending, default False → standard rejection
        adapter._send_dispatch_rejection(intent, "some_other_reason")

        feedback = mock_sink.put_nowait.call_args[0][0]
        assert feedback.was_approved is False


class TestMaxPosNotBreachedOnPhantomRace:
    """End-to-end-ish: simulate the exact 21:26:57-58 race and verify pending preserved."""

    def test_two_dispatches_with_phantom_flag_preserve_pending(self):
        """Simulate: two SELL intents both dispatch-fail and become phantoms.
        With the fix, pending_sell stays at 1 after both feedbacks (same order)
        because was_approved=True skips decrement."""
        strat = _make_r47()
        # Mimic emit path: pending_sell incremented on .sell() call
        strat._pending_sell["TMFE6"] = 1  # after first SELL emit

        # First dispatch_failed, with fix adapter flags was_approved=True
        strat.on_risk_feedback(_make_risk_feedback(Side.SELL, "dispatch_failed", was_approved=True))
        assert strat._pending_sell["TMFE6"] == 1, "After fix: first dispatch_failed keeps pending elevated"

        # R47 emits second SELL — can_sell uses pending_sell=1, pos=0
        # → can_sell = 0 - 1 > -1 (which is False when max_pos=1)
        # So second SELL should NOT be emitted. But let's assume it is (simulating
        # bug that already happened). pending_sell=2.
        strat._pending_sell["TMFE6"] = 2

        # Second dispatch_failed feedback also flagged
        strat.on_risk_feedback(_make_risk_feedback(Side.SELL, "dispatch_failed", was_approved=True))
        assert strat._pending_sell["TMFE6"] == 2, "Second dispatch_failed also preserves pending"

        # When fills arrive for both phantoms, on_fill decrements naturally

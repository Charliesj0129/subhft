"""D3 (2026-04-21 incident, minimal): track all in-flight order ids as a set
so that RTT-spike scenarios cannot orphan earlier orders when multiple
SUBMITTED callbacks arrive between placements.

The full D3 (intent-id contract change in OrderEvent) is deferred; this
minimal D3 provides defense-in-depth via set-valued tracking using the
existing broker-oid interface.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.strategy import Side
from hft_platform.events import GapEvent, LOBStatsEvent


def _lob_stats(mid_pts=37759, spread_pts=3):
    return LOBStatsEvent(
        symbol="TMFE6",
        ts=0,
        imbalance=0.0,
        best_bid=(mid_pts - spread_pts // 2) * 10000,
        best_ask=(mid_pts + spread_pts // 2 + spread_pts % 2) * 10000,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=mid_pts * 2 * 10000,
        spread_scaled=spread_pts * 10000,
    )


def _order(order_id, side, status=OrderStatus.SUBMITTED, price=3_7754_0000):
    return OrderEvent(
        order_id=order_id,
        strategy_id="r47_test",
        symbol="TMFE6",
        status=status,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=1,
        price=price,
        side=side,
        ingest_ts_ns=0,
        broker_ts_ns=0,
    )


def _fill(order_id, side, price=3_7754_0000):
    return FillEvent(
        fill_id=f"F-{order_id}",
        account_id="acct",
        order_id=order_id,
        strategy_id="r47_test",
        symbol="TMFE6",
        side=side,
        qty=1,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


def _ctx():
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_test"
    ctx.place_order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
    return ctx


@pytest.fixture()
def strategy():
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    s = R47MakerStrategy(
        strategy_id="r47_test",
        pe_danger_threshold=0.0,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        qi_skew_threshold=1.0,
        max_pos=1,
        stale_quote_max_ticks=3,
        quote_cooldown_ms=0,  # disable for this test
    )
    s.symbols = {"TMFE6"}
    return s


class TestInflightOidSet:
    def test_two_submitted_both_tracked(self, strategy):
        """RTT-spike case: two SUBMITTED callbacks arrive in sequence; BOTH
        oids must be tracked, not just the last one."""
        ctx = _ctx()
        strategy.handle_event(ctx, _order("oA", Side.BUY))
        strategy.handle_event(ctx, _order("oB", Side.BUY))
        assert strategy._inflight_buy_oids.get("TMFE6", set()) == {"oA", "oB"}, (
            "D3: both in-flight BUY oids must be tracked as a set"
        )

    def test_cancelled_removes_only_that_oid(self, strategy):
        ctx = _ctx()
        strategy.handle_event(ctx, _order("oA", Side.BUY))
        strategy.handle_event(ctx, _order("oB", Side.BUY))
        strategy.handle_event(ctx, _order("oA", Side.BUY, status=OrderStatus.CANCELLED))
        assert strategy._inflight_buy_oids.get("TMFE6", set()) == {"oB"}, "D3: CANCELLED(oA) must leave oB tracked"

    def test_fill_removes_only_that_oid(self, strategy):
        ctx = _ctx()
        strategy.handle_event(ctx, _order("oA", Side.BUY))
        strategy.handle_event(ctx, _order("oB", Side.BUY))
        strategy.handle_event(ctx, _fill("oA", Side.BUY))
        assert strategy._inflight_buy_oids.get("TMFE6", set()) == {"oB"}, "D3: fill(oA) must leave oB tracked"

    def test_fill_prefix_order_id_removes_registered_oid(self, strategy):
        """Shioaji fill ordno can extend the submitted order ordno.

        Live audit observed order_id ``v007o`` with fill broker_order_id
        ``v007oW1D``. The fill must still remove the tracked submitted oid;
        otherwise the strategy later sends stale cancels for an already-filled
        order.
        """
        ctx = _ctx()
        strategy.handle_event(ctx, _order("v007o", Side.BUY))
        strategy.handle_event(ctx, _fill("v007oW1D", Side.BUY))
        assert strategy._inflight_buy_oids.get("TMFE6", set()) == set()

    def test_gap_clears_all_inflight(self, strategy):
        ctx = _ctx()
        strategy.handle_event(ctx, _order("oA", Side.BUY))
        strategy.handle_event(ctx, _order("oB", Side.SELL))
        strategy.handle_event(ctx, GapEvent(missed_count=3, first_missed_seq=1, last_missed_seq=3, ts=0))
        assert strategy._inflight_buy_oids.get("TMFE6", set()) == set()
        assert strategy._inflight_sell_oids.get("TMFE6", set()) == set()

    def test_reconcile_cancels_all_stale_inflight(self, strategy):
        """When multiple BUY oids are in flight and all are stale relative to
        current mid, reconcile must fire a cancel intent for EACH oid."""
        ctx = _ctx()
        # Seed two in-flight BUY orders at 37754.
        strategy.handle_event(ctx, _order("oA", Side.BUY))
        strategy.handle_event(ctx, _order("oB", Side.BUY))
        strategy._last_bid["TMFE6"] = 3_7754_0000
        # Note: _active_buy_oid still single-slot — set by the LAST SUBMITTED.
        # Reconcile should use the inflight SET, not just _active_buy_oid.

        # Mid now 37760 → 6 pts stale.
        ev = _lob_stats(mid_pts=37760, spread_pts=3)
        intents = strategy.handle_event(ctx, ev)

        from hft_platform.contracts.strategy import IntentType

        cancels = [i for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL]
        target_ids = {getattr(i, "target_order_id", None) for i in cancels}
        assert target_ids >= {"oA", "oB"}, (
            f"D3: reconcile must cancel ALL stale in-flight oids; cancelled targets = {target_ids}"
        )

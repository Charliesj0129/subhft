"""Bug 9: R47 max_pos=1 violated by consecutive LOBStats events.

Reproduction: 3 LOBStats events with spread>=5 arrive in rapid succession.
Each has a slightly different mid_price, causing bid_moved/ask_moved=True.
R47 should generate at most 1 buy + 1 sell (2 intents) with max_pos=1,
but was observed generating 3 pairs (6 intents) in production.

Root cause: pending counter must prevent re-quoting after the first pair.
Defense-in-depth: even if pending is somehow reset, the guard must hold.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent

_PRICE_SCALE = 10000


def _make_lob_stats(
    symbol="TMFE6",
    mid_price_x2=7462_0000,
    spread_scaled=5_0000,
    imbalance=0.0,
    best_bid=3728_0000,
    best_ask=3733_0000,
):
    return LOBStatsEvent(
        symbol=symbol,
        ts=0,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
    )


def _make_feature_event(symbol="TMFE6", quality_flags=0):
    values = tuple([0] * 22)
    return FeatureUpdateEvent(
        symbol=symbol,
        ts=0,
        local_ts=0,
        seq=1,
        feature_set_id="lob_shared_v3",
        schema_version=3,
        changed_mask=0,
        warmup_ready_mask=0,
        quality_flags=quality_flags,
        feature_ids=tuple(f"f{i}" for i in range(len(values))),
        values=values,
    )


def _make_ctx():
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_test"
    # Return a unique mock for each place_order call
    ctx.place_order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
    return ctx


@pytest.fixture()
def r47_max1():
    """R47 with max_pos=1, all safety gates disabled (matching prod config)."""
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy(
        strategy_id="r47_test",
        pe_danger_threshold=0.0,
        pe_window=100,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        qi_skew_threshold=0.10,
        qi_widen_ticks=1,
        max_pos=1,
    )
    strat.symbols = {"TMFE6"}
    return strat


class TestBug9MaxPosViolation:
    """Verify R47 never exceeds max_pos=1 under consecutive spread>=5 events."""

    def test_consecutive_lobstats_respects_max_pos(self, r47_max1):
        """3 consecutive LOBStats with spread>=5 and different prices:
        only the FIRST should produce buy+sell intents."""
        ctx = _make_ctx()

        # LOBStats events with slightly different mid prices (different quotes each time)
        events = [
            _make_lob_stats(mid_price_x2=7462_0000, spread_scaled=5_0000),
            _make_lob_stats(mid_price_x2=7461_0000, spread_scaled=5_0000),
            _make_lob_stats(mid_price_x2=7460_0000, spread_scaled=5_0000),
        ]

        all_intents = []
        for ev in events:
            intents = r47_max1.handle_event(ctx, ev)
            all_intents.extend(intents)

        # max_pos=1: at most 1 buy + 1 sell = 2 intents total
        assert len(all_intents) <= 2, (
            f"Bug 9: max_pos=1 violated! Got {len(all_intents)} intents "
            f"from {len(events)} consecutive LOBStats events. "
            f"Expected <= 2 (1 buy + 1 sell)."
        )

    def test_pending_counter_blocks_second_quote(self, r47_max1):
        """After first buy+sell, pending counters must block subsequent quotes."""
        ctx = _make_ctx()

        # First LOBStats: should generate buy + sell
        ev1 = _make_lob_stats(mid_price_x2=7462_0000, spread_scaled=5_0000)
        intents1 = r47_max1.handle_event(ctx, ev1)
        assert len(intents1) == 2, f"First event should generate 2 intents, got {len(intents1)}"

        # Check pending counters
        assert r47_max1._pending_buy.get("TMFE6", 0) == 1
        assert r47_max1._pending_sell.get("TMFE6", 0) == 1

        # Second LOBStats with different price: should be blocked
        ev2 = _make_lob_stats(mid_price_x2=7461_0000, spread_scaled=5_0000)
        intents2 = r47_max1.handle_event(ctx, ev2)
        assert len(intents2) == 0, f"Second event should be blocked, got {len(intents2)} intents"

    def test_fill_unlocks_next_quote(self, r47_max1):
        """After both sides fill, the next LOBStats should generate quotes again."""
        ctx = _make_ctx()

        # Generate first pair
        ev1 = _make_lob_stats(mid_price_x2=7462_0000, spread_scaled=5_0000)
        intents1 = r47_max1.handle_event(ctx, ev1)
        assert len(intents1) == 2

        # Simulate fills
        buy_fill = FillEvent(
            fill_id="F001",
            account_id="acct",
            order_id="O001",
            strategy_id="r47_test",
            symbol="TMFE6",
            side=Side.BUY,
            qty=1,
            price=3727_0000,
            fee=0,
            tax=0,
            ingest_ts_ns=0,
            match_ts_ns=0,
        )
        sell_fill = FillEvent(
            fill_id="F002",
            account_id="acct",
            order_id="O002",
            strategy_id="r47_test",
            symbol="TMFE6",
            side=Side.SELL,
            qty=1,
            price=3733_0000,
            fee=0,
            tax=0,
            ingest_ts_ns=0,
            match_ts_ns=0,
        )
        r47_max1.handle_event(ctx, buy_fill)
        r47_max1.handle_event(ctx, sell_fill)

        assert r47_max1._pending_buy.get("TMFE6", 0) == 0
        assert r47_max1._pending_sell.get("TMFE6", 0) == 0
        assert r47_max1._local_pos.get("TMFE6", 0) == 0

        # Next LOBStats: should generate quotes again
        ev2 = _make_lob_stats(mid_price_x2=7460_0000, spread_scaled=5_0000)
        intents2 = r47_max1.handle_event(ctx, ev2)
        assert len(intents2) == 2, f"After fills, next event should generate 2 intents, got {len(intents2)}"

    def test_risk_feedback_with_int_side_decrements_correctly(self, r47_max1):
        """Verify RiskFeedback with Side enum (not int) decrements the right counter."""
        ctx = _make_ctx()

        # Generate quotes
        ev1 = _make_lob_stats(mid_price_x2=7462_0000, spread_scaled=5_0000)
        r47_max1.handle_event(ctx, ev1)
        assert r47_max1._pending_buy.get("TMFE6", 0) == 1

        # Send risk feedback with Side.BUY enum
        fb = RiskFeedback(
            intent_id=1,
            strategy_id="r47_test",
            symbol="TMFE6",
            reason_code="REJECTED",
            timestamp_ns=0,
            side=Side.BUY,
            was_approved=False,
        )
        r47_max1.on_risk_feedback(fb)
        assert r47_max1._pending_buy.get("TMFE6", 0) == 0

    def test_risk_feedback_with_int_side_falls_through(self, r47_max1):
        """Bug 9 hypothesis: RiskFeedback with int side (from typed intent)
        falls through to else branch, decrementing BOTH counters."""
        ctx = _make_ctx()

        # Generate quotes
        ev1 = _make_lob_stats(mid_price_x2=7462_0000, spread_scaled=5_0000)
        r47_max1.handle_event(ctx, ev1)
        assert r47_max1._pending_buy.get("TMFE6", 0) == 1
        assert r47_max1._pending_sell.get("TMFE6", 0) == 1

        # Send risk feedback with int side (as typed_intent_identity returns)
        fb = RiskFeedback(
            intent_id=1,
            strategy_id="r47_test",
            symbol="TMFE6",
            reason_code="TRACK_GATE_SESSION_FILTERED",
            timestamp_ns=0,
            side=int(Side.BUY),  # int, not Side enum!
            was_approved=False,
        )
        r47_max1.on_risk_feedback(fb)

        # If Side is IntEnum: int(Side.BUY) == Side.BUY → True, only buy decremented
        # If Side is regular Enum: int(Side.BUY) != Side.BUY → else branch, BOTH decremented!
        # This is the potential root cause of Bug 9.
        buy_pending = r47_max1._pending_buy.get("TMFE6", 0)
        sell_pending = r47_max1._pending_sell.get("TMFE6", 0)

        # CORRECT behavior: only buy should be decremented
        assert buy_pending == 0, f"Buy pending should be 0, got {buy_pending}"
        assert sell_pending == 1, f"Sell pending should still be 1, got {sell_pending}"

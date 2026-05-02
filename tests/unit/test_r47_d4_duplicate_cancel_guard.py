"""D4 (2026-04-21 incident, verification): duplicate-cancel guard.

Today's 210 ``cancel_already_terminal`` events came from re-firing cancel
against the same oid every time ``bid_moved`` was True — the old code did
NOT clear the oid after dispatching cancel, so the next tick saw the same
oid still tracked.

D1+D3 together now clear both ``_active_*_oid`` and ``_inflight_*_oids``
after a reconcile-triggered cancel. This test pins that behavior: fire 5
stale-trigger events against the same oid, expect exactly ONE cancel intent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.events import LOBStatsEvent


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
        quote_cooldown_ms=0,
    )
    s.symbols = {"TMFE6"}
    return s


class TestDuplicateCancelGuard:
    def test_stale_buy_cancelled_exactly_once_across_repeated_events(self, strategy):
        """5 consecutive stale-trigger events: exactly 1 cancel intent, not 5."""
        ctx = _ctx()
        strategy._last_bid["TMFE6"] = 3_7754_0000
        strategy._active_buy_oid["TMFE6"] = "v004N"
        strategy._inflight_buy_oids["TMFE6"] = {"v004N"}

        total_cancels = 0
        for _ in range(5):
            ev = _lob_stats(mid_pts=37760, spread_pts=3)
            intents = strategy.handle_event(ctx, ev)
            total_cancels += sum(1 for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL)

        assert total_cancels == 1, (
            f"D4: duplicate-cancel guard violated — expected 1 cancel across "
            f"5 repeated stale events, got {total_cancels}. This reproduces "
            f"the 210 cancel_already_terminal events seen on 2026-04-21."
        )

    def test_price_moved_cancelled_exactly_once_across_repeated_events(self, strategy):
        """The legacy cancel-before-requote path must also clear oid state.

        This covers the non-D1 path: spread gate passes, the quote is still
        close enough to mid to avoid stale reconciliation, but the computed
        quote price moved by one tick. Before the fix this path re-cancelled
        the same oid on every tick until the broker terminal callback arrived.
        """
        ctx = _ctx()
        strategy._last_bid["TMFE6"] = 3_7758_0000
        strategy._active_buy_oid["TMFE6"] = "v004N"
        strategy._inflight_buy_oids["TMFE6"] = {"v004N"}
        strategy._pending_buy["TMFE6"] = 1

        total_cancels = 0
        for _ in range(5):
            ev = _lob_stats(mid_pts=37761, spread_pts=8)
            intents = strategy.handle_event(ctx, ev)
            total_cancels += sum(1 for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL)

        assert total_cancels == 1, (
            f"D4: price-moved cancel path must also clear oid state; "
            f"expected 1 cancel across repeated ticks, got {total_cancels}"
        )

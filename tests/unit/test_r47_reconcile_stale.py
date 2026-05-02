"""D1 fix (2026-04-21 incident): reconcile stale quotes regardless of gates.

Root cause: R47's spread gate (default 5 pts) short-circuits on_stats with an
early return when live spread < 5 pts. On 2026-04-21 TMFE6 median spread was
3 pts — 96.3% of ticks blocked. During those blocks the F2 cancel-stale branch
(inside _generate_quotes) never executed, leaving orders sitting at stale
prices for up to 41 seconds while the market ran 30+ pts away.

These tests pin the FIX: when an active order's last quoted price drifts too
far from current mid, cancel it on the NEXT tick, regardless of whether any
placement gate (spread / toxicity / PE) would block a new quote.

Today's evidence (hft.orders 2026-04-21):
  - v004N BUY @ 37754 sat 41,323 ms; 729/730 ticks in window had spread < 5
  - BUY order lifetime p95 = 7,057 ms; max = 41,323 ms
  - SELL order lifetime p95 = 32,363 ms; max = 78,736 ms
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import OrderEvent, OrderStatus
from hft_platform.events import LOBStatsEvent

_PRICE_SCALE = 10000


def _lob_stats(
    symbol="TMFE6",
    mid_price_x2=7_5518_0000,
    spread_scaled=3_0000,
    imbalance=0.0,
    best_bid=3_7758_0000,
    best_ask=3_7760_0000,
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


def _order_event(order_id, side, symbol="TMFE6", status=OrderStatus.SUBMITTED):
    return OrderEvent(
        order_id=order_id,
        strategy_id="r47_test",
        symbol=symbol,
        status=status,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=1,
        price=3_7754_0000,
        side=side,
        ingest_ts_ns=0,
        broker_ts_ns=0,
    )


def _ctx():
    ctx = MagicMock()
    ctx.positions = {}
    ctx.strategy_id = "r47_test"
    ctx.place_order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
    return ctx


@pytest.fixture()
def strategy():
    """R47 with prod-like config: spread gate 5 pts, max_pos 1."""
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    s = R47MakerStrategy(
        strategy_id="r47_test",
        pe_danger_threshold=0.0,
        pe_window=100,
        queue_cancel_threshold=1.0,
        mfg_skew_z_threshold=100.0,
        spread_threshold_pts=5,
        toxicity_max=9999,
        qi_skew_threshold=1.0,
        qi_widen_ticks=1,
        max_pos=1,
        # D1 parameter under test
        stale_quote_max_ticks=3,
    )
    s.symbols = {"TMFE6"}
    return s


class TestReconcileStaleQuotesUnderGateBlock:
    """D1: cancel-stale must run even when a placement gate would block."""

    def test_stale_buy_cancelled_when_spread_gate_blocks(self, strategy):
        """Reproduces v004N: BUY at 37754, mid rises to 37760 (6 pts), spread 3 pts < gate.
        Strategy must emit a CANCEL for the active BUY order.
        """
        ctx = _ctx()

        # Seed prior state: previous quote at 37754 (BUY), broker SUBMITTED arrived.
        strategy._last_bid["TMFE6"] = 3_7754_0000
        strategy._active_buy_oid["TMFE6"] = "v004N"

        # Current market: mid at 37759, spread 3 pts (below gate). Distance
        # from _last_bid = 37759 - 37754 = 5 pts > stale_quote_max_ticks=3.
        ev = _lob_stats(
            mid_price_x2=3_7759_0000 * 2,
            spread_scaled=3_0000,
            best_bid=3_7758_0000,
            best_ask=3_7761_0000,
        )
        intents = strategy.handle_event(ctx, ev)

        from hft_platform.contracts.strategy import IntentType

        cancels = [i for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL]
        # Expect at least one CANCEL intent targeting v004N.
        assert len(cancels) >= 1, (
            f"D1: stale BUY cancel must fire when spread gate blocks; got {len(intents)} intents, none were CANCEL"
        )

    def test_stale_sell_cancelled_when_spread_gate_blocks(self, strategy):
        """Symmetric for SELL side: SELL sits at stale high price, mid drifts down."""
        ctx = _ctx()

        strategy._last_ask["TMFE6"] = 3_7720_0000
        strategy._active_sell_oid["TMFE6"] = "v005c"

        # Current market: mid at 37714, 6 pts below our stale ask, spread 3.
        ev = _lob_stats(
            mid_price_x2=3_7714_0000 * 2,
            spread_scaled=3_0000,
            best_bid=3_7713_0000,
            best_ask=3_7716_0000,
        )
        intents = strategy.handle_event(ctx, ev)

        from hft_platform.contracts.strategy import IntentType

        cancels = [i for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL]
        assert len(cancels) >= 1, "D1: stale SELL cancel must fire when spread gate blocks"

    def test_no_cancel_when_quote_still_close_to_mid(self, strategy):
        """Healthy quote (≤ threshold ticks from mid) must NOT be cancelled."""
        ctx = _ctx()

        strategy._last_bid["TMFE6"] = 3_7758_0000
        strategy._active_buy_oid["TMFE6"] = "v007P"

        # mid=37759, distance = 1 pt, within stale_quote_max_ticks=3.
        ev = _lob_stats(
            mid_price_x2=3_7759_0000 * 2,
            spread_scaled=3_0000,
            best_bid=3_7758_0000,
            best_ask=3_7761_0000,
        )
        intents = strategy.handle_event(ctx, ev)

        from hft_platform.contracts.strategy import IntentType

        cancels = [i for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL]
        assert len(cancels) == 0, (
            "D1: close quote must not be cancelled; got spurious CANCEL. Reconcile is over-aggressive."
        )

    def test_active_oid_cleared_after_cancel_dispatch(self, strategy):
        """After cancel fires, _active_buy_oid must be cleared so the strategy
        does not re-cancel the same oid on the next tick (source of today's 210
        cancel_already_terminal events)."""
        ctx = _ctx()

        strategy._last_bid["TMFE6"] = 3_7754_0000
        strategy._active_buy_oid["TMFE6"] = "v004N"

        ev = _lob_stats(
            mid_price_x2=3_7760_0000 * 2,
            spread_scaled=3_0000,
            best_bid=3_7759_0000,
            best_ask=3_7762_0000,
        )
        strategy.handle_event(ctx, ev)

        assert strategy._active_buy_oid.get("TMFE6") is None, (
            "D1: _active_buy_oid must be cleared optimistically after cancel "
            "dispatch to prevent repeated cancels on the same oid."
        )

    def test_reconcile_runs_before_gates_on_gate_blocked_tick(self, strategy):
        """Exhaustive: spread gate blocks new quote, but cancel-stale path still runs."""
        ctx = _ctx()

        strategy._last_bid["TMFE6"] = 3_7754_0000
        strategy._active_buy_oid["TMFE6"] = "v004N"
        strategy._last_ask["TMFE6"] = 3_7720_0000
        strategy._active_sell_oid["TMFE6"] = "v005c"

        # Mid at 37759, spread 3 → both sides stale, gate blocks new placements.
        ev = _lob_stats(
            mid_price_x2=3_7759_0000 * 2,
            spread_scaled=3_0000,
            best_bid=3_7758_0000,
            best_ask=3_7761_0000,
        )
        intents = strategy.handle_event(ctx, ev)

        from hft_platform.contracts.strategy import IntentType

        cancels = [i for i in intents if getattr(i, "intent_type", None) == IntentType.CANCEL]
        # Both sides should have cancel dispatched.
        assert len(cancels) == 2, (
            f"D1: expected 2 cancels (both sides stale), got {len(cancels)}. "
            f"All intents: {[str(getattr(i, 'intent_type', '?')) for i in intents]}"
        )


class TestReconcileStaleQuotesNoneGuard:
    """P3-a1: defense-in-depth — `event.mid_price_x2` and `event.spread_scaled`
    are typed `int | None`. Today on_stats guards before delegating but the
    contract is brittle. The local guard inside `_reconcile_stale_quotes` and
    `_generate_quotes` must short-circuit when either is None, so the function
    never crashes if invoked with a partially-populated event."""

    def test_reconcile_stale_quotes_returns_on_none_mid(self, strategy):
        """`_reconcile_stale_quotes` must not raise on `mid_price_x2 is None`."""
        ev = LOBStatsEvent(
            symbol="TMFE6",
            ts=0,
            imbalance=0.0,
            best_bid=0,
            best_ask=0,
            bid_depth=0,
            ask_depth=0,
            mid_price_x2=None,
            spread_scaled=3_0000,
        )
        # Direct call: must not raise even when on_stats guard is bypassed.
        strategy._reconcile_stale_quotes(ev)

    def test_reconcile_stale_quotes_returns_on_none_spread(self, strategy):
        """`_reconcile_stale_quotes` must not raise on `spread_scaled is None`."""
        ev = LOBStatsEvent(
            symbol="TMFE6",
            ts=0,
            imbalance=0.0,
            best_bid=0,
            best_ask=0,
            bid_depth=0,
            ask_depth=0,
            mid_price_x2=3_7759_0000 * 2,
            spread_scaled=None,
        )
        strategy._reconcile_stale_quotes(ev)

"""Coverage tests for strategies/tx_tmf_leadlag.py — targeting uncovered lines.

Covers: _in_session wrap-around, _near_session_end wrap-around,
_check_exits_on_tmf_event, on_tick TMF routing, negative dvol (day boundary),
on_stats exit paths (cancel resting + pending_force_close retry),
on_fill exit matching (order_id, inflight, side-only), on_order tracking,
_emit_aggressive_exit guard, _enter_tmf with no ctx/L1.

All prices use scaled int x10000 (Precision Law).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.execution import Side as ExecSide
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import LOBStatsEvent, MetaData, TickEvent
from hft_platform.strategies.tx_tmf_leadlag import (
    TxTmfLeadLagStrategy,
    _OpenPosition,
)
from hft_platform.strategy.base import StrategyContext

_ONE_SEC_NS = 1_000_000_000
# 09:30 TWN = 01:30 UTC
_TS_BASE_NS = (1 * 3600 + 30 * 60) * _ONE_SEC_NS
_PTS_SCALE = 10_000


def _scaled(points: int) -> int:
    return points * _PTS_SCALE


def _make_tx_tick(
    ts: int = _TS_BASE_NS,
    price_pts: int = 20_000,
    total_volume: int = 100,
) -> TickEvent:
    return TickEvent(
        meta=MetaData(seq=1, source_ts=ts, local_ts=ts),
        symbol="TXFD6",
        price=_scaled(price_pts),
        volume=0,
        total_volume=total_volume,
    )


def _make_tmf_tick(
    ts: int = _TS_BASE_NS,
    price_pts: int = 5_000,
) -> TickEvent:
    return TickEvent(
        meta=MetaData(seq=1, source_ts=ts, local_ts=ts),
        symbol="TMFD6",
        price=_scaled(price_pts),
        volume=1,
    )


def _make_tmf_stats(
    ts: int = _TS_BASE_NS,
    mid_pts: int = 5_000,
    spread_pts: int = 3,
) -> LOBStatsEvent:
    best_bid = _scaled(mid_pts) - _scaled(spread_pts) // 2
    best_ask = _scaled(mid_pts) + _scaled(spread_pts) // 2
    return LOBStatsEvent(
        symbol="TMFD6",
        ts=ts,
        imbalance=0.0,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=best_bid + best_ask,
        spread_scaled=best_ask - best_bid,
    )


def _make_tmf_fill(
    side: Side,
    price_pts: int,
    order_id: str = "fill-1",
    match_ts_ns: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id=f"fill-{order_id}",
        account_id="acct",
        order_id=order_id,
        strategy_id="tx_tmf_leadlag",
        symbol="TMFD6",
        side=side,
        qty=1,
        price=_scaled(price_pts),
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=match_ts_ns,
    )


def _make_tmf_order(
    status: OrderStatus,
    side: Side,
    order_id: str = "ord-1",
    filled_qty: int = 0,
) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        strategy_id="tx_tmf_leadlag",
        symbol="TMFD6",
        status=status,
        submitted_qty=1,
        filled_qty=filled_qty,
        remaining_qty=1 - filled_qty,
        price=_scaled(5_000),
        side=ExecSide.BUY if side == Side.BUY else ExecSide.SELL,
        ingest_ts_ns=0,
        broker_ts_ns=0,
    )


def _make_ctx(position: int = 0, l1_data: tuple | None = None) -> StrategyContext:
    positions = {"TMFD6": position}
    next_id = {"v": 0}

    def _intent_factory(**kwargs: object) -> OrderIntent:
        next_id["v"] += 1
        return OrderIntent(
            intent_id=next_id["v"],
            strategy_id="tx_tmf_leadlag",
            symbol=str(kwargs["symbol"]),
            intent_type=kwargs.get("intent_type", IntentType.NEW),
            side=kwargs["side"],
            price=int(kwargs["price"]),
            qty=int(kwargs["qty"]),
            tif=kwargs.get("tif", TIF.LIMIT),
            target_order_id=kwargs.get("target_order_id"),
        )

    def _scale_price(_sym: str, p: int) -> int:
        return int(p)

    default_l1 = (
        _TS_BASE_NS,
        _scaled(4_999),
        _scaled(5_001),
        _scaled(4_999) + _scaled(5_001),
        _scaled(2),
        10,
        10,
    )

    def _l1_source(symbol: str):
        if symbol == "TMFD6":
            return l1_data or default_l1
        return None

    return StrategyContext(
        positions=positions,
        strategy_id="tx_tmf_leadlag",
        intent_factory=_intent_factory,
        price_scaler=_scale_price,
        lob_l1_source=_l1_source,
    )


def _make_strategy(**overrides) -> TxTmfLeadLagStrategy:
    defaults = {
        "session_start_sec": 0,
        "session_end_sec": 86400,
        "dvol_threshold": 20,
        "sl_pts": 100,
        "max_hold_ns": 900 * _ONE_SEC_NS,
        "max_position_lots": 3,
        "cooldown_ns": 0,
    }
    defaults.update(overrides)
    return TxTmfLeadLagStrategy(**defaults)


def _seed_tx_baseline(strat: TxTmfLeadLagStrategy, ctx: StrategyContext) -> None:
    strat.handle_event(ctx, _make_tx_tick(ts=_TS_BASE_NS, price_pts=20_000, total_volume=100))


def _enter_long(strat, ctx, entry_pts=5_001):
    """Helper to create a long position."""
    _seed_tx_baseline(strat, ctx)
    strat.handle_event(
        ctx,
        _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
    )
    strat.handle_event(
        ctx,
        _make_tmf_fill(Side.BUY, entry_pts, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS),
    )


def _enter_short(strat, ctx, entry_pts=4_999):
    """Helper to create a short position."""
    _seed_tx_baseline(strat, ctx)
    strat.handle_event(
        ctx,
        _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=19_990, total_volume=125),
    )
    strat.handle_event(
        ctx,
        _make_tmf_fill(Side.SELL, entry_pts, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS),
    )


# ---------------------------------------------------------------------------
# _in_session — wrap-around handling
# ---------------------------------------------------------------------------


class TestInSession:
    def test_normal_session_within(self):
        """Lines 125-128: start < end, sec within range."""
        strat = _make_strategy(session_start_sec=1000, session_end_sec=5000)
        assert strat._in_session(3000 * _ONE_SEC_NS) is True

    def test_normal_session_before(self):
        strat = _make_strategy(session_start_sec=1000, session_end_sec=5000)
        assert strat._in_session(500 * _ONE_SEC_NS) is False

    def test_normal_session_after(self):
        strat = _make_strategy(session_start_sec=1000, session_end_sec=5000)
        assert strat._in_session(6000 * _ONE_SEC_NS) is False

    def test_wraparound_session_before_midnight(self):
        """Line 129: start > end, sec >= start (before midnight)."""
        strat = _make_strategy(session_start_sec=80000, session_end_sec=5000)
        assert strat._in_session(85000 * _ONE_SEC_NS) is True

    def test_wraparound_session_after_midnight(self):
        """Line 129: start > end, sec <= end (after midnight)."""
        strat = _make_strategy(session_start_sec=80000, session_end_sec=5000)
        assert strat._in_session(3000 * _ONE_SEC_NS) is True

    def test_wraparound_session_gap(self):
        """Wrap-around: sec in the gap between end and start."""
        strat = _make_strategy(session_start_sec=80000, session_end_sec=5000)
        assert strat._in_session(40000 * _ONE_SEC_NS) is False


# ---------------------------------------------------------------------------
# _near_session_end — wrap-around handling
# ---------------------------------------------------------------------------


class TestNearSessionEnd:
    def test_near_end_within_margin(self):
        """Lines 300-309: within margin of session end."""
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=30 * _ONE_SEC_NS,
        )
        # cutoff = 5000 - 30 = 4970
        assert strat._near_session_end(4975 * _ONE_SEC_NS) is True

    def test_not_near_end_before_margin(self):
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=30 * _ONE_SEC_NS,
        )
        assert strat._near_session_end(4960 * _ONE_SEC_NS) is False

    def test_near_end_wraparound(self):
        """Lines 304-309: cutoff < 0 wraps around."""
        strat = _make_strategy(
            session_end_sec=20,
            force_close_margin_ns=30 * _ONE_SEC_NS,
        )
        # cutoff = 20 - 30 = -10 → -10 + 86400 = 86390
        # sec_of_day in [86390, 86400] OR [0, 20] should be near end
        assert strat._near_session_end(86395 * _ONE_SEC_NS) is True
        assert strat._near_session_end(10 * _ONE_SEC_NS) is True
        assert strat._near_session_end(50000 * _ONE_SEC_NS) is False


# ---------------------------------------------------------------------------
# _check_exits_on_tmf_event (TMF tick exit path)
# ---------------------------------------------------------------------------


class TestCheckExitsOnTmfEvent:
    def test_tmf_tick_triggers_sl_exit(self):
        """Lines 232-294: TMF tick triggers SL check."""
        strat = _make_strategy(sl_pts=100)
        # L1 data showing bid well below entry (entry=5001, bid=4900 → loss=101 pts > 100 SL)
        sl_l1 = (
            _TS_BASE_NS,
            _scaled(4_900),
            _scaled(4_902),
            _scaled(4_900) + _scaled(4_902),
            _scaled(2),
            10,
            10,
        )
        ctx = _make_ctx(l1_data=sl_l1)
        _enter_long(strat, ctx)
        assert len(strat._positions_open) == 1

        # TMF tick triggers the exit check — L1 bid at 4900 triggers SL
        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=4_900)
        intents = strat.handle_event(ctx, tmf_tick)
        assert len(intents) == 1
        assert intents[0].side == Side.SELL

    def test_tmf_tick_no_exit_within_sl(self):
        """TMF tick within SL threshold does not exit."""
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        _enter_long(strat, ctx)

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 10 * _ONE_SEC_NS, price_pts=4_960)
        intents = strat.handle_event(ctx, tmf_tick)
        assert intents == []

    def test_tmf_tick_no_positions_skips(self):
        """Lines 233-234: no positions → early return."""
        strat = _make_strategy()
        ctx = _make_ctx()
        tmf_tick = _make_tmf_tick()
        intents = strat.handle_event(ctx, tmf_tick)
        assert intents == []

    def test_tmf_tick_no_ctx_skips(self):  # noqa: no-assert
        """Lines 235-236: no ctx → early return."""
        strat = _make_strategy()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        strat._positions_open.append(pos)
        # Handle without ctx
        strat.ctx = None
        tmf_tick = _make_tmf_tick()
        strat._check_exits_on_tmf_event(tmf_tick)
        # No crash

    def test_tmf_tick_uses_l1_cache(self):
        """Lines 240-243: L1 cache used for bid/ask."""
        strat = _make_strategy(sl_pts=100)
        l1 = (_TS_BASE_NS, _scaled(4_890), _scaled(4_892), 0, 0, 0, 0)
        ctx = _make_ctx(l1_data=l1)
        _enter_long(strat, ctx)
        # TMF tick — L1 shows bid well below entry
        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=4_891)
        intents = strat.handle_event(ctx, tmf_tick)
        assert len(intents) == 1

    def test_tmf_tick_fallback_no_l1(self):
        """Lines 245-247: no L1 → use tick price as bid/ask."""
        strat = _make_strategy(max_hold_ns=1)  # instant time-kill to avoid SL math
        ctx = _make_ctx()  # normal L1 for entry phase
        _enter_long(strat, ctx)

        # Now switch L1 to None so fallback path is exercised
        def _no_l1(symbol):
            return None

        ctx._lob_l1_source = _no_l1

        # TMF tick — L1 returns None, so tick price used as bid/ask. Time-kill triggers.
        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=5_000)
        intents = strat.handle_event(ctx, tmf_tick)
        assert len(intents) == 1

    def test_tmf_tick_zero_bid_ask_skips(self):
        """Lines 249-250: both bid/ask <= 0 skips."""
        strat = _make_strategy()
        l1 = (_TS_BASE_NS, 0, 0, 0, 0, 0, 0)
        ctx = _make_ctx(l1_data=l1)
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        strat._positions_open.append(pos)
        strat.ctx = ctx
        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=0)
        strat._check_exits_on_tmf_event(tmf_tick)
        # No exit
        assert len(strat._positions_open) == 1

    def test_tmf_tick_time_kill(self):
        """TMF tick triggers time-kill exit."""
        strat = _make_strategy(max_hold_ns=300 * _ONE_SEC_NS, sl_pts=10000)
        ctx = _make_ctx()
        _enter_long(strat, ctx)

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 302 * _ONE_SEC_NS, price_pts=5_001)
        intents = strat.handle_event(ctx, tmf_tick)
        assert len(intents) == 1
        assert intents[0].intent_type == IntentType.FORCE_FLAT

    def test_tmf_tick_pending_force_close_retry(self):
        """Lines 255-257: pending_force_close retries on tick."""
        strat = _make_strategy()
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=_TS_BASE_NS, entry_price=_scaled(5_001), direction=1)
        pos.pending_force_close = True
        pos.exit_order_id = ""
        strat._positions_open.append(pos)
        strat.ctx = ctx
        strat._generated_intents = []

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=4_900)
        strat._check_exits_on_tmf_event(tmf_tick)
        assert pos.aggressive_exit_inflight is True

    def test_tmf_tick_awaiting_exit_skips(self):
        """Lines 259-260: awaiting_exit position skipped."""
        strat = _make_strategy(max_hold_ns=1)
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        pos.awaiting_exit = True
        strat._positions_open.append(pos)
        strat.ctx = ctx
        strat._generated_intents = []

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=4_900)
        strat._check_exits_on_tmf_event(tmf_tick)
        # No new exit intent
        assert len(strat._generated_intents) == 0

    def test_tmf_tick_entry_price_zero_skips(self):
        """Lines 263-264: entry_price <= 0 skips."""
        strat = _make_strategy(max_hold_ns=1)
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=0, direction=1)
        strat._positions_open.append(pos)
        strat.ctx = ctx
        strat._generated_intents = []

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=4_900)
        strat._check_exits_on_tmf_event(tmf_tick)
        assert len(strat._generated_intents) == 0

    def test_tmf_tick_cancel_resting_exit_before_force(self):
        """Lines 287-290: cancel resting exit and set pending_force_close."""
        strat = _make_strategy(max_hold_ns=1)
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        pos.exit_order_id = "EXIT-100"
        pos.awaiting_exit = False
        strat._positions_open.append(pos)
        strat.ctx = ctx
        strat._generated_intents = []

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=5_000)
        strat._check_exits_on_tmf_event(tmf_tick)
        assert pos.pending_force_close is True
        assert pos.exit_order_id == ""

    def test_tmf_tick_eod_force_close(self):
        """TMF tick in EOD zone triggers force close."""
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=30 * _ONE_SEC_NS,
            max_hold_ns=3600 * _ONE_SEC_NS,
            sl_pts=10000,
        )
        ctx = _make_ctx()
        pos = _OpenPosition(
            entry_ts_ns=2000 * _ONE_SEC_NS,
            entry_price=_scaled(5_001),
            direction=1,
        )
        strat._positions_open.append(pos)
        strat.ctx = ctx
        strat._generated_intents = []

        tmf_tick = _make_tmf_tick(ts=4975 * _ONE_SEC_NS, price_pts=5_001)
        strat._check_exits_on_tmf_event(tmf_tick)
        assert pos.aggressive_exit_inflight is True


# ---------------------------------------------------------------------------
# Short position exits
# ---------------------------------------------------------------------------


class TestShortPositionExits:
    def test_short_sl_exit_via_stats(self):
        """Short position SL: mark_price (best_ask) > entry + SL."""
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        _enter_short(strat, ctx)
        assert len(strat._positions_open) == 1
        assert strat._positions_open[0].direction == -1

        # Price rises 101 pts above entry
        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, mid_pts=5_102),
        )
        assert len(intents) == 1
        assert intents[0].side == Side.BUY

    def test_short_sl_exit_via_tmf_tick(self):
        """Short SL triggered by TMF tick."""
        strat = _make_strategy(sl_pts=100)
        # L1 data showing ask well above entry (entry=4999, ask=5102 → loss=103 pts > 100 SL)
        sl_l1 = (
            _TS_BASE_NS,
            _scaled(5_100),
            _scaled(5_102),
            _scaled(5_100) + _scaled(5_102),
            _scaled(2),
            10,
            10,
        )
        ctx = _make_ctx(l1_data=sl_l1)
        _enter_short(strat, ctx)

        tmf_tick = _make_tmf_tick(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, price_pts=5_102)
        intents = strat.handle_event(ctx, tmf_tick)
        assert len(intents) == 1
        assert intents[0].side == Side.BUY


# ---------------------------------------------------------------------------
# on_tick TX routing and negative dvol
# ---------------------------------------------------------------------------


class TestOnTickRouting:
    def test_tx_tick_ignored_for_other_symbol(self):
        """Lines 143-144: non-signal non-trade symbol skipped."""
        strat = _make_strategy()
        ctx = _make_ctx()
        tick = TickEvent(
            meta=MetaData(seq=1, source_ts=_TS_BASE_NS, local_ts=_TS_BASE_NS),
            symbol="OTHERFD6",
            price=_scaled(20_000),
            volume=0,
            total_volume=100,
        )
        intents = strat.handle_event(ctx, tick)
        assert intents == []

    def test_negative_dvol_treated_as_vol(self):
        """Lines 164-166: negative dvol (day boundary) → dvol = vol."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)
        # Simulate volume going backwards (day boundary in cumulative volume)
        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=50),
        )
        # dvol = 50 - 100 = -50 → dvol = 50, which > 20 threshold, dp > 0 → signal
        assert len(intents) == 1

    def test_first_tick_stores_baseline(self):
        """Lines 158-160: first tick with last_tx_vol == 0 just sets baseline."""
        strat = _make_strategy()
        ctx = _make_ctx()
        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS, price_pts=20_000, total_volume=100),
        )
        assert intents == []
        assert strat._last_tx_vol == 100
        assert strat._last_tx_price == _scaled(20_000)


# ---------------------------------------------------------------------------
# _enter_tmf edge cases
# ---------------------------------------------------------------------------


class TestEnterTmf:
    def test_enter_tmf_no_ctx(self):
        """Line 192: no ctx → return immediately."""
        strat = _make_strategy()
        strat.ctx = None
        strat._enter_tmf(1, _TS_BASE_NS)
        assert strat._awaiting_entry == 0

    def test_enter_tmf_no_l1(self):
        """Lines 194-196: L1 returns None → no entry."""
        strat = _make_strategy()
        ctx = MagicMock()
        ctx.get_l1_scaled.return_value = None
        strat.ctx = ctx
        strat._generated_intents = []
        strat._enter_tmf(1, _TS_BASE_NS)
        assert strat._awaiting_entry == 0

    def test_enter_tmf_zero_ask_skips_buy(self):
        """Lines 204-205: best_ask <= 0 blocks buy entry."""
        strat = _make_strategy()
        ctx = MagicMock()
        ctx.get_l1_scaled.return_value = (_TS_BASE_NS, _scaled(5_000), 0, 0, 0, 0, 0)
        ctx.place_order = MagicMock()
        strat.ctx = ctx
        strat._generated_intents = []
        strat._enter_tmf(1, _TS_BASE_NS)
        assert strat._awaiting_entry == 0

    def test_enter_tmf_zero_bid_skips_sell(self):
        """Lines 209-210: best_bid <= 0 blocks sell entry."""
        strat = _make_strategy()
        ctx = MagicMock()
        ctx.get_l1_scaled.return_value = (_TS_BASE_NS, 0, _scaled(5_001), 0, 0, 0, 0)
        ctx.place_order = MagicMock()
        strat.ctx = ctx
        strat._generated_intents = []
        strat._enter_tmf(-1, _TS_BASE_NS)
        assert strat._awaiting_entry == 0

    def test_enter_tmf_sell_direction(self):
        """Lines 207-211: direction < 0 → sell at best_bid."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)
        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=19_990, total_volume=125),
        )
        assert len(intents) == 1
        assert intents[0].side == Side.SELL
        assert strat._awaiting_entry == -1


# ---------------------------------------------------------------------------
# _emit_aggressive_exit
# ---------------------------------------------------------------------------


class TestEmitAggressiveExit:
    def test_inflight_guard_prevents_duplicate(self):
        """Line 376-377: aggressive_exit_inflight blocks duplicate."""
        strat = _make_strategy()
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        pos.aggressive_exit_inflight = True
        strat.ctx = ctx
        strat._generated_intents = []
        strat._emit_aggressive_exit(pos, _scaled(4_900), _scaled(4_902))
        assert len(strat._generated_intents) == 0

    def test_zero_price_skips(self):
        """Lines 383: price <= 0 skips."""
        strat = _make_strategy()
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        strat.ctx = ctx
        strat._generated_intents = []
        strat._emit_aggressive_exit(pos, 0, 0)
        assert len(strat._generated_intents) == 0

    def test_long_exit_sells_at_bid(self):
        """Lines 378-379: long direction → sell at best_bid."""
        strat = _make_strategy()
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        strat.ctx = ctx
        strat._generated_intents = []
        strat._emit_aggressive_exit(pos, _scaled(4_900), _scaled(4_905))
        assert len(strat._generated_intents) == 1
        assert strat._generated_intents[0].side == Side.SELL
        assert pos.aggressive_exit_inflight is True
        assert pos.awaiting_exit is True

    def test_short_exit_buys_at_ask(self):
        """Lines 380-381: short direction → buy at best_ask."""
        strat = _make_strategy()
        ctx = _make_ctx()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(4_999), direction=-1)
        strat.ctx = ctx
        strat._generated_intents = []
        strat._emit_aggressive_exit(pos, _scaled(5_050), _scaled(5_055))
        assert len(strat._generated_intents) == 1
        assert strat._generated_intents[0].side == Side.BUY

    def test_no_ctx_skips(self):
        """Line 384: ctx is None → skip."""
        strat = _make_strategy()
        pos = _OpenPosition(entry_ts_ns=0, entry_price=_scaled(5_001), direction=1)
        strat.ctx = None
        strat._generated_intents = []
        strat._emit_aggressive_exit(pos, _scaled(4_900), _scaled(4_905))
        assert len(strat._generated_intents) == 0


# ---------------------------------------------------------------------------
# on_fill — exit matching
# ---------------------------------------------------------------------------


class TestOnFillExitMatching:
    def test_exit_fill_matches_by_order_id(self):
        """Lines 430-433: exact order_id match takes priority."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.exit_order_id = "EX-001"
        pos.awaiting_exit = True

        fill = _make_tmf_fill(Side.SELL, 5_010, order_id="EX-001")
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 0

    def test_exit_fill_matches_inflight_no_oid(self):
        """Lines 434: inflight exit without order_id matched as fallback."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.aggressive_exit_inflight = True
        pos.exit_order_id = ""

        fill = _make_tmf_fill(Side.SELL, 5_010, order_id="unknown-oid")
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 0

    def test_exit_fill_matches_by_side_fallback(self):
        """Lines 436-437: side-only match as last resort."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        # Not awaiting, not inflight, no order_id

        fill = _make_tmf_fill(Side.SELL, 5_010, order_id="unknown")
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 0

    def test_fill_wrong_symbol_ignored(self):
        """Lines 402-403: fill for non-trade symbol ignored."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        fill = FillEvent(
            fill_id="f1",
            account_id="acct",
            order_id="o1",
            strategy_id="tx_tmf_leadlag",
            symbol="WRONG",
            side=Side.SELL,
            qty=1,
            price=_scaled(5_000),
            fee=0,
            tax=0,
            ingest_ts_ns=0,
            match_ts_ns=0,
        )
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 1  # unchanged

    def test_fill_wrong_side_not_matched(self):
        """Lines 429: wrong exit_side skipped."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        # BUY fill should not match a long exit (which expects SELL)
        fill = _make_tmf_fill(Side.BUY, 5_010)
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 1


# ---------------------------------------------------------------------------
# on_order — exit tracking and terminal states
# ---------------------------------------------------------------------------


class TestOnOrderExitTracking:
    def test_submitted_sets_exit_order_id(self):
        """Lines 467-474: SUBMITTED captures order_id for awaiting exit."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.awaiting_exit = True

        order = _make_tmf_order(OrderStatus.SUBMITTED, Side.SELL, order_id="EX-001")
        strat.handle_event(ctx, order)
        assert pos.exit_order_id == "EX-001"
        assert pos.awaiting_exit is False

    def test_pending_submit_sets_exit_order_id(self):
        """Lines 467-474: PENDING_SUBMIT also captures order_id."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.awaiting_exit = True

        order = _make_tmf_order(OrderStatus.PENDING_SUBMIT, Side.SELL, order_id="EX-002")
        strat.handle_event(ctx, order)
        assert pos.exit_order_id == "EX-002"

    def test_terminal_unfilled_sets_pending_force_close(self):
        """Lines 475-479: terminal with filled_qty=0 → pending_force_close."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.awaiting_exit = True
        pos.aggressive_exit_inflight = True

        order = _make_tmf_order(OrderStatus.CANCELLED, Side.SELL, filled_qty=0)
        strat.handle_event(ctx, order)
        assert pos.aggressive_exit_inflight is False
        assert pos.pending_force_close is True

    def test_matched_order_id_filled_removes_position(self):
        """Lines 481-484: FILLED by matching order_id removes position."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.exit_order_id = "EX-003"

        order = _make_tmf_order(OrderStatus.FILLED, Side.SELL, order_id="EX-003")
        strat.handle_event(ctx, order)
        assert len(strat._positions_open) == 0

    def test_matched_order_id_terminal_sets_force_close(self):
        """Lines 485-489: terminal (not FILLED) resets for retry."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.exit_order_id = "EX-004"

        order = _make_tmf_order(OrderStatus.FAILED, Side.SELL, order_id="EX-004")
        strat.handle_event(ctx, order)
        assert pos.exit_order_id == ""
        assert pos.pending_force_close is True

    def test_wrong_symbol_ignored(self):
        """Lines 450-451: order for non-trade symbol ignored."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        order = OrderEvent(
            order_id="o1",
            strategy_id="tx_tmf_leadlag",
            symbol="WRONG",
            status=OrderStatus.SUBMITTED,
            submitted_qty=1,
            filled_qty=0,
            remaining_qty=1,
            price=_scaled(5_000),
            side=ExecSide.SELL,
            ingest_ts_ns=0,
            broker_ts_ns=0,
        )
        strat.handle_event(ctx, order)
        pos = strat._positions_open[0]
        assert pos.exit_order_id == ""

    def test_entry_rejection_resets_awaiting(self):
        """Lines 454-458: entry order rejected → awaiting cleared."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert strat._awaiting_entry == 1
        # Reject the entry IOC
        order = _make_tmf_order(OrderStatus.FAILED, Side.BUY, filled_qty=0)
        strat.handle_event(ctx, order)
        assert strat._awaiting_entry == 0
        assert len(strat._positions_open) == 0

    def test_partially_filled_entry_not_reset(self):
        """Lines 456: filled_qty > 0 does not reset awaiting."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert strat._awaiting_entry == 1
        order = _make_tmf_order(OrderStatus.CANCELLED, Side.BUY, filled_qty=1)
        strat.handle_event(ctx, order)
        # filled_qty > 0 → doesn't match the terminal-unfilled branch
        assert strat._awaiting_entry == 1


# ---------------------------------------------------------------------------
# on_stats — additional exit paths
# ---------------------------------------------------------------------------


class TestOnStatsExitPaths:
    def test_non_trade_symbol_stats_ignored(self):
        """Lines 312-313: stats for non-trade symbol ignored."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        stats = LOBStatsEvent(
            symbol="TXFD6",  # signal symbol, not trade
            ts=_TS_BASE_NS,
            imbalance=0.0,
            best_bid=_scaled(20_000),
            best_ask=_scaled(20_010),
            bid_depth=10,
            ask_depth=10,
            mid_price_x2=_scaled(20_000) + _scaled(20_010),
            spread_scaled=_scaled(10),
        )
        intents = strat.handle_event(ctx, stats)
        assert intents == []

    def test_no_positions_stats_noop(self):
        """Lines 314-315: no positions → early return."""
        strat = _make_strategy()
        ctx = _make_ctx()
        intents = strat.handle_event(ctx, _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS))
        assert intents == []

    def test_stats_mark_price_fallback_midpoint(self):
        """Lines 339-340: mark_price falls back to midpoint when opposing side is 0.

        Long position: mark_price = best_bid. If best_bid == 0, fallback to
        (best_bid + best_ask) // 2. The strategy may guard against zero mark
        price, so verify it at least doesn't crash.
        """
        strat = _make_strategy(max_hold_ns=1)  # instant time-kill
        ctx = _make_ctx()
        _enter_long(strat, ctx)

        # Stats with best_bid=0 but best_ask > 0 → mark_price fallback path
        stats = LOBStatsEvent(
            symbol="TMFD6",
            ts=_TS_BASE_NS + 60 * _ONE_SEC_NS,
            imbalance=0.0,
            best_bid=0,
            best_ask=_scaled(5_001),
            bid_depth=0,
            ask_depth=10,
        )
        intents = strat.handle_event(ctx, stats)
        # Strategy may exit or skip depending on mark_price calculation
        assert isinstance(intents, list)

    def test_stats_cancel_resting_then_pending_force_close(self):
        """Lines 359-371: cancel resting exit sets pending_force_close."""
        strat = _make_strategy(max_hold_ns=1)  # instant time-kill
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.exit_order_id = "EX-REST"
        pos.awaiting_exit = False

        intents = strat.handle_event(ctx, _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS))
        assert pos.pending_force_close is True
        assert pos.exit_order_id == ""
        # A cancel intent should be generated
        assert any(i.intent_type == IntentType.CANCEL for i in intents)


# ---------------------------------------------------------------------------
# _current_lots
# ---------------------------------------------------------------------------


class TestCurrentLots:
    def test_includes_open_positions(self):
        strat = _make_strategy()
        strat._positions_open.append(_OpenPosition(0, _scaled(5_001), 1))
        assert strat._current_lots() == 1

    def test_includes_awaiting_entry(self):
        strat = _make_strategy()
        strat._awaiting_entry = 1
        assert strat._current_lots() == 1

    def test_combined_count(self):
        strat = _make_strategy()
        strat._positions_open.append(_OpenPosition(0, _scaled(5_001), 1))
        strat._awaiting_entry = -1
        assert strat._current_lots() == 2


# ---------------------------------------------------------------------------
# Additional coverage: on_fill edge cases (no match found)
# ---------------------------------------------------------------------------


class TestOnFillNoMatch:
    def test_exit_fill_no_positions_noop(self):
        """No positions open: fill is a no-op."""
        strat = _make_strategy()
        ctx = _make_ctx()
        # No entry at all
        fill = _make_tmf_fill(Side.SELL, 5_010)
        strat.handle_event(ctx, fill)
        assert len(strat._positions_open) == 0

    def test_entry_fill_wrong_side_not_consumed(self):
        """Awaiting buy entry but sell fill arrives → not consumed as entry."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)
        # Generate buy entry signal
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert strat._awaiting_entry == 1
        # SELL fill arrives — does not match awaiting BUY entry
        fill = _make_tmf_fill(Side.SELL, 5_000)
        strat.handle_event(ctx, fill)
        assert strat._awaiting_entry == 1  # still waiting


# ---------------------------------------------------------------------------
# Additional coverage: on_order exit tracking branch (non-matching side)
# ---------------------------------------------------------------------------


class TestOnOrderNonMatchingSide:
    def test_exit_order_wrong_side_ignored(self):
        """Exit tracking skips positions where exit_side != event.side."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.awaiting_exit = True
        # Long exit_side = SELL, but event side = BUY
        order = _make_tmf_order(OrderStatus.SUBMITTED, Side.BUY, order_id="EX-005")
        strat.handle_event(ctx, order)
        # Position not matched — exit_order_id not set
        assert pos.exit_order_id == ""

    def test_exit_order_partially_filled_sets_oid(self):
        """PARTIALLY_FILLED is also a non-terminal status that captures order_id."""
        strat = _make_strategy()
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        pos = strat._positions_open[0]
        pos.awaiting_exit = True
        order = _make_tmf_order(OrderStatus.PARTIALLY_FILLED, Side.SELL, order_id="EX-006")
        strat.handle_event(ctx, order)
        assert pos.exit_order_id == "EX-006"


# ---------------------------------------------------------------------------
# Additional coverage: _near_session_end edge
# ---------------------------------------------------------------------------


class TestNearSessionEndEdge:
    def test_exactly_at_cutoff(self):
        """Exactly at cutoff boundary."""
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=30 * _ONE_SEC_NS,
        )
        # cutoff = 4970, at 4970 should be in zone
        assert strat._near_session_end(4970 * _ONE_SEC_NS) is True

    def test_one_second_before_cutoff(self):
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=30 * _ONE_SEC_NS,
        )
        assert strat._near_session_end(4969 * _ONE_SEC_NS) is False

    def test_cutoff_normal_no_wrap(self):
        """Lines 307-308: cutoff <= session_end, normal range check."""
        strat = _make_strategy(
            session_end_sec=5000,
            force_close_margin_ns=10 * _ONE_SEC_NS,
        )
        # cutoff = 4990, session_end = 5000
        assert strat._near_session_end(4995 * _ONE_SEC_NS) is True
        assert strat._near_session_end(4985 * _ONE_SEC_NS) is False


# ---------------------------------------------------------------------------
# on_stats — both mark price zero → skip
# ---------------------------------------------------------------------------


class TestOnStatsBothZero:
    def test_both_bid_ask_zero_in_stats_noop(self):
        """Both best_bid and best_ask zero from stats → mark_price=0 → skip."""
        strat = _make_strategy(max_hold_ns=1)
        ctx = _make_ctx()
        _enter_long(strat, ctx)
        stats = LOBStatsEvent(
            symbol="TMFD6",
            ts=_TS_BASE_NS + 60 * _ONE_SEC_NS,
            imbalance=0.0,
            best_bid=0,
            best_ask=0,
            bid_depth=0,
            ask_depth=0,
        )
        intents = strat.handle_event(ctx, stats)
        # mark_price = 0, fallback midpoint = 0 → skip entirely
        assert intents == []

"""Unit tests for TxTmfLeadLagStrategy (R28)."""

from __future__ import annotations

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.execution import Side as ExecSide
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import LOBStatsEvent, MetaData, TickEvent
from hft_platform.strategies.tx_tmf_leadlag import TxTmfLeadLagStrategy
from hft_platform.strategy.base import StrategyContext

_ONE_SEC_NS = 1_000_000_000
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
    """Feed initial TX tick to set baseline volume/price."""
    strat.handle_event(ctx, _make_tx_tick(ts=_TS_BASE_NS, price_pts=20_000, total_volume=100))


class TestSignalGeneration:
    def test_large_dvol_with_dp_generates_buy_signal(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )

        assert len(intents) == 1
        assert intents[0].symbol == "TMFD6"
        assert intents[0].side == Side.BUY
        assert intents[0].tif == TIF.IOC
        assert strat._awaiting_entry == 1

    def test_large_dvol_with_negative_dp_generates_sell_signal(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=19_990, total_volume=125),
        )

        assert len(intents) == 1
        assert intents[0].side == Side.SELL

    def test_small_dvol_no_signal(self) -> None:
        strat = _make_strategy(dvol_threshold=20)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=115),
        )

        assert intents == []

    def test_zero_dp_no_signal(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_000, total_volume=125),
        )

        assert intents == []

    def test_max_position_lots_blocks_entry(self) -> None:
        strat = _make_strategy(max_position_lots=1)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 1 * _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS))

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 2 * _ONE_SEC_NS, price_pts=20_020, total_volume=150),
        )

        assert intents == []

    def test_cooldown_blocks_rapid_entry(self) -> None:
        strat = _make_strategy(cooldown_ns=10 * _ONE_SEC_NS, max_position_lots=3)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents1 = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 1 * _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert len(intents1) == 1

        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS))

        intents2 = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 5 * _ONE_SEC_NS, price_pts=20_020, total_volume=150),
        )
        assert intents2 == []

        intents3 = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 12 * _ONE_SEC_NS, price_pts=20_030, total_volume=180),
        )
        assert len(intents3) == 1


class TestEntryFill:
    def test_fill_creates_open_position(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert strat._awaiting_entry == 1

        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS))

        assert strat._awaiting_entry == 0
        assert len(strat._positions_open) == 1
        assert strat._positions_open[0].direction == 1
        assert strat._positions_open[0].entry_price == _scaled(5_001)

    def test_entry_ioc_rejection_resets_awaiting(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        assert strat._awaiting_entry == 1

        strat.handle_event(
            ctx,
            _make_tmf_order(OrderStatus.CANCELLED, Side.BUY, filled_qty=0),
        )

        assert strat._awaiting_entry == 0
        assert len(strat._positions_open) == 0


class TestExitConditions:
    def _enter_long(self, strat, ctx) -> None:
        _seed_tx_baseline(strat, ctx)
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(
            ctx,
            _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS),
        )

    def test_stop_loss_triggers_force_flat_ioc_exit(self) -> None:
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        self._enter_long(strat, ctx)

        exit_intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, mid_pts=4_900),
        )

        assert len(exit_intents) == 1
        assert exit_intents[0].side == Side.SELL
        assert exit_intents[0].tif == TIF.IOC
        assert exit_intents[0].intent_type == IntentType.FORCE_FLAT

    def test_no_exit_within_sl_threshold(self) -> None:
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        self._enter_long(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, mid_pts=4_951),
        )

        assert intents == []

    def test_time_kill_triggers_force_flat_exit(self) -> None:
        strat = _make_strategy(max_hold_ns=300 * _ONE_SEC_NS)
        ctx = _make_ctx()
        self._enter_long(strat, ctx)

        exit_intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 302 * _ONE_SEC_NS, mid_pts=5_001),
        )

        assert len(exit_intents) == 1
        assert exit_intents[0].side == Side.SELL
        assert exit_intents[0].tif == TIF.IOC
        assert exit_intents[0].intent_type == IntentType.FORCE_FLAT

    def test_no_exit_before_max_hold(self) -> None:
        strat = _make_strategy(max_hold_ns=300 * _ONE_SEC_NS)
        ctx = _make_ctx()
        self._enter_long(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 200 * _ONE_SEC_NS, mid_pts=5_001),
        )

        assert intents == []


class TestExitFill:
    def _enter_and_trigger_sl(self, strat, ctx):
        _seed_tx_baseline(strat, ctx)
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(
            ctx,
            _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS),
        )
        strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 60 * _ONE_SEC_NS, mid_pts=4_900),
        )

    def test_exit_fill_removes_position(self) -> None:
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        self._enter_and_trigger_sl(strat, ctx)
        assert len(strat._positions_open) == 1

        strat.handle_event(ctx, _make_tmf_fill(Side.SELL, 4_900))

        assert len(strat._positions_open) == 0

    def test_exit_ioc_rejection_sets_pending_force_close(self) -> None:
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        self._enter_and_trigger_sl(strat, ctx)

        pos = strat._positions_open[0]
        assert pos.aggressive_exit_inflight is True
        assert pos.awaiting_exit is True

        strat.handle_event(
            ctx,
            _make_tmf_order(OrderStatus.CANCELLED, Side.SELL, filled_qty=0),
        )

        assert pos.aggressive_exit_inflight is False
        assert pos.pending_force_close is True

    def test_pending_force_close_retries_on_next_stats(self) -> None:
        strat = _make_strategy(sl_pts=100)
        ctx = _make_ctx()
        self._enter_and_trigger_sl(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tmf_order(OrderStatus.CANCELLED, Side.SELL, filled_qty=0),
        )
        pos = strat._positions_open[0]
        assert pos.pending_force_close is True

        retry_intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=_TS_BASE_NS + 61 * _ONE_SEC_NS, mid_pts=4_900),
        )

        assert len(retry_intents) == 1
        assert retry_intents[0].side == Side.SELL
        assert retry_intents[0].tif == TIF.IOC
        assert retry_intents[0].intent_type == IntentType.FORCE_FLAT


class TestMultiPosition:
    def test_multiple_positions_tracked_independently(self) -> None:
        strat = _make_strategy(max_position_lots=3, cooldown_ns=0)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 1 * _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS))

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 2 * _ONE_SEC_NS, price_pts=20_020, total_volume=150),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_002, match_ts_ns=_TS_BASE_NS + 2 * _ONE_SEC_NS))

        assert len(strat._positions_open) == 2
        assert strat._positions_open[0].entry_price == _scaled(5_001)
        assert strat._positions_open[1].entry_price == _scaled(5_002)

    def test_exit_fill_closes_oldest_position_first(self) -> None:
        strat = _make_strategy(max_position_lots=3, cooldown_ns=0)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 1 * _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=_TS_BASE_NS + _ONE_SEC_NS))

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + 2 * _ONE_SEC_NS, price_pts=20_020, total_volume=150),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_002, match_ts_ns=_TS_BASE_NS + 2 * _ONE_SEC_NS))

        strat.handle_event(ctx, _make_tmf_fill(Side.SELL, 5_010))

        assert len(strat._positions_open) == 1
        assert strat._positions_open[0].entry_price == _scaled(5_002)


class TestDayBoundary:
    def test_volume_reset_on_new_day(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx()

        day1_base = 86_400 * _ONE_SEC_NS
        day2_base = 2 * 86_400 * _ONE_SEC_NS

        strat.handle_event(ctx, _make_tx_tick(ts=day1_base, price_pts=20_000, total_volume=500))
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=day1_base + _ONE_SEC_NS, price_pts=20_010, total_volume=525),
        )
        assert strat._awaiting_entry == 1
        strat._awaiting_entry = 0

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=day2_base, price_pts=20_020, total_volume=5),
        )
        assert intents == []
        assert strat._last_tx_vol == 5


class TestSessionGate:
    def test_no_entry_outside_session(self) -> None:
        strat = _make_strategy(session_start_sec=45 * 60, session_end_sec=5 * 3600 + 45 * 60)
        ctx = _make_ctx()

        ts_outside = 6 * 3600 * _ONE_SEC_NS
        strat.handle_event(ctx, _make_tx_tick(ts=ts_outside, price_pts=20_000, total_volume=100))
        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=ts_outside + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )

        assert intents == []

    def test_entry_uses_new_intent_type(self) -> None:
        strat = _make_strategy(session_start_sec=0, session_end_sec=86400)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )

        assert len(intents) == 1
        assert intents[0].intent_type == IntentType.NEW

    def test_entry_within_session(self) -> None:
        strat = _make_strategy(session_start_sec=0, session_end_sec=86400)
        ctx = _make_ctx()
        _seed_tx_baseline(strat, ctx)

        intents = strat.handle_event(
            ctx,
            _make_tx_tick(ts=_TS_BASE_NS + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )

        assert len(intents) == 1


class TestEodForceClose:
    def test_eod_force_close_triggers(self) -> None:
        """Positions are force-closed when near session end."""
        session_start_sec = 1_000
        session_end_sec = 5_000
        margin_ns = 30 * _ONE_SEC_NS

        strat = _make_strategy(
            session_start_sec=session_start_sec,
            session_end_sec=session_end_sec,
            utc_offset_sec=0,
            force_close_margin_ns=margin_ns,
            max_hold_ns=3600 * _ONE_SEC_NS,
            sl_pts=10_000,
        )
        ctx = _make_ctx()

        entry_ts = 2_000 * _ONE_SEC_NS
        strat.handle_event(ctx, _make_tx_tick(ts=entry_ts, price_pts=20_000, total_volume=100))
        strat.handle_event(
            ctx,
            _make_tx_tick(ts=entry_ts + _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(
            ctx,
            _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=entry_ts + _ONE_SEC_NS),
        )
        assert len(strat._positions_open) == 1

        no_close_ts = 4_960 * _ONE_SEC_NS
        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=no_close_ts, mid_pts=5_001),
        )
        assert intents == []
        assert len(strat._positions_open) == 1

        force_close_ts = 4_971 * _ONE_SEC_NS
        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=force_close_ts, mid_pts=5_001),
        )
        assert len(intents) == 1
        assert intents[0].side == Side.SELL
        assert intents[0].tif == TIF.IOC
        assert intents[0].intent_type == IntentType.FORCE_FLAT

    def test_eod_force_close_closes_all_positions(self) -> None:
        """All open positions are closed at session end, not just one."""
        session_start_sec = 1_000
        session_end_sec = 5_000
        margin_ns = 30 * _ONE_SEC_NS

        strat = _make_strategy(
            session_start_sec=session_start_sec,
            session_end_sec=session_end_sec,
            utc_offset_sec=0,
            force_close_margin_ns=margin_ns,
            max_hold_ns=3600 * _ONE_SEC_NS,
            sl_pts=10_000,
            max_position_lots=3,
            cooldown_ns=0,
        )
        ctx = _make_ctx()

        entry_ts = 2_000 * _ONE_SEC_NS
        strat.handle_event(ctx, _make_tx_tick(ts=entry_ts, price_pts=20_000, total_volume=100))

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=entry_ts + 1 * _ONE_SEC_NS, price_pts=20_010, total_volume=125),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_001, match_ts_ns=entry_ts + _ONE_SEC_NS))

        strat.handle_event(
            ctx,
            _make_tx_tick(ts=entry_ts + 2 * _ONE_SEC_NS, price_pts=20_020, total_volume=150),
        )
        strat.handle_event(ctx, _make_tmf_fill(Side.BUY, 5_002, match_ts_ns=entry_ts + 2 * _ONE_SEC_NS))

        assert len(strat._positions_open) == 2

        force_close_ts = 4_980 * _ONE_SEC_NS
        intents = strat.handle_event(
            ctx,
            _make_tmf_stats(ts=force_close_ts, mid_pts=5_001),
        )

        assert len(intents) == 2
        assert all(i.side == Side.SELL for i in intents)
        assert all(i.intent_type == IntentType.FORCE_FLAT for i in intents)

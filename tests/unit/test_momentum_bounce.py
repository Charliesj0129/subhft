"""Unit tests for MomentumBounceStrategy — CBS-flipped with trailing stop."""

from __future__ import annotations

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.momentum_bounce import MomentumBounceStrategy
from hft_platform.strategy.base import StrategyContext

_ONE_SEC_NS = 1_000_000_000
_TS_0930_UTC_NS = 5400 * _ONE_SEC_NS


def _mid_x2(points: int) -> int:
    return points * 20_000


def _scaled_price(points: int) -> int:
    return points * 10_000


def _make_stats(
    symbol: str = "TMFD6",
    ts: int = _TS_0930_UTC_NS,
    points: int = 33_000,
    spread_pts: int = 1,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=_scaled_price(points) - _scaled_price(spread_pts) // 2,
        best_ask=_scaled_price(points) + _scaled_price(spread_pts) // 2,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=_mid_x2(points),
        spread_scaled=_scaled_price(spread_pts),
    )


def _make_ctx(position: int = 0) -> StrategyContext:
    positions = {"TMFD6": position}
    next_intent_id = {"value": 0}

    def _intent_factory(**kwargs: object) -> OrderIntent:
        next_intent_id["value"] += 1
        return OrderIntent(
            intent_id=next_intent_id["value"],
            strategy_id="momentum_bounce",
            symbol=str(kwargs["symbol"]),
            intent_type=kwargs.get("intent_type", IntentType.NEW),
            side=kwargs["side"],
            price=int(kwargs["price"]),
            qty=int(kwargs["qty"]),
            tif=kwargs.get("tif", TIF.LIMIT),
            target_order_id=kwargs.get("target_order_id"),
        )

    def _scale_price(_symbol: str, price: int) -> int:
        return int(price)

    return StrategyContext(
        positions=positions,
        strategy_id="momentum_bounce",
        intent_factory=_intent_factory,
        price_scaler=_scale_price,
    )


def _make_fill(side: Side, price_points: int, order_id: str = "entry-1") -> FillEvent:
    return FillEvent(
        fill_id=f"fill-{order_id}",
        account_id="acct",
        order_id=order_id,
        strategy_id="momentum_bounce",
        symbol="TMFD6",
        side=side,
        qty=1,
        price=_scaled_price(price_points),
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


def _make_strategy(**kwargs: object) -> MomentumBounceStrategy:
    defaults = {
        "strategy_id": "momentum_bounce",
        "lookback_ns": 60 * _ONE_SEC_NS,
        "trigger_sigma": 3.0,
        "max_hold_ns": 900 * _ONE_SEC_NS,
        "stop_loss_pts": 10,
        "trailing_stop_pts": 6,
        "min_vol_samples": 8,
        "max_spread_pts": 3,
        "session_start_sec": 0,
        "session_end_sec": 86400,
    }
    defaults.update(kwargs)
    return MomentumBounceStrategy(**defaults)


def _seed_low_vol(
    strat: MomentumBounceStrategy,
    ctx: StrategyContext,
    base_pts: int = 33_000,
    base_ts: int = _TS_0930_UTC_NS,
) -> None:
    """Feed 20 ticks with 1-pt oscillation to establish stable low-vol baseline.

    This matches the CBS test pattern: enough ticks that a subsequent 10+ pt move
    exceeds 3-sigma without the move itself dominating the RMS estimate.
    """
    for i in range(20):
        pts = base_pts + (1 if i % 2 else 0)
        strat.handle_event(ctx, _make_stats(ts=base_ts + i * _ONE_SEC_NS, points=pts))


def _enter_long(strat: MomentumBounceStrategy, ctx: StrategyContext) -> None:
    """Seed low-vol, trigger upward momentum entry at +12 pts, fill it."""
    _seed_low_vol(strat, ctx)
    ts_trigger = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
    strat.handle_event(ctx, _make_stats(ts=ts_trigger, points=33_012))
    fill = _make_fill(Side.BUY, 33_012)
    strat.handle_event(ctx, fill)


# ---------------------------------------------------------------------------
# Direction flip tests
# ---------------------------------------------------------------------------


def test_momentum_enters_with_upward_move():
    """After large upward move, momentum should BUY (not SELL like CBS)."""
    strat = _make_strategy()
    ctx = _make_ctx()
    _seed_low_vol(strat, ctx)

    ts_trigger = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_trigger, points=33_012))

    assert len(intents) == 1
    assert intents[0].side == Side.BUY  # Momentum: buy on up move
    assert intents[0].tif == TIF.IOC


def test_momentum_enters_with_downward_move():
    """After large downward move, momentum should SELL (not BUY like CBS)."""
    strat = _make_strategy()
    ctx = _make_ctx()
    _seed_low_vol(strat, ctx)

    ts_trigger = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_trigger, points=32_990))

    assert len(intents) == 1
    assert intents[0].side == Side.SELL  # Momentum: sell on down move
    assert intents[0].tif == TIF.IOC


# ---------------------------------------------------------------------------
# No passive TP on fill
# ---------------------------------------------------------------------------


def test_no_passive_tp_placed_on_entry_fill():
    """Momentum should NOT place a passive take-profit order after fill."""
    strat = _make_strategy()
    ctx = _make_ctx()
    _seed_low_vol(strat, ctx)

    ts_trigger = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
    strat.handle_event(ctx, _make_stats(ts=ts_trigger, points=33_012))

    fill = _make_fill(Side.BUY, 33_012)
    intents = strat.handle_event(ctx, fill)

    assert len(intents) == 0
    assert strat._state["TMFD6"] == "positioned"


# ---------------------------------------------------------------------------
# Trailing stop tests
# ---------------------------------------------------------------------------


def test_trailing_stop_exits_after_peak():
    """Should exit when PnL drops trailing_stop_pts below peak."""
    strat = _make_strategy(stop_loss_pts=20, trailing_stop_pts=6, max_hold_ns=3600 * _ONE_SEC_NS)
    ctx = _make_ctx()
    _enter_long(strat, ctx)
    assert strat._state["TMFD6"] == "positioned"

    # Price rises to 33027 (+15 pts profit from entry at 33012)
    ts_rise = _TS_0930_UTC_NS + 40 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_rise, points=33_027))
    assert len(intents) == 0
    assert strat._peak_pnl_scaled["TMFD6"] > 0

    # Price drops to 33020 (+8 pts from entry; peak was ~15, dropped ~7 > trailing 6)
    ts_drop = _TS_0930_UTC_NS + 50 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_drop, points=33_020))
    assert len(intents) == 1
    assert intents[0].side == Side.SELL
    assert intents[0].tif == TIF.IOC


def test_hard_stop_loss_exits():
    """Should exit on hard stop-loss even if peak never went positive."""
    strat = _make_strategy(stop_loss_pts=10, trailing_stop_pts=6, max_hold_ns=3600 * _ONE_SEC_NS)
    ctx = _make_ctx()
    _enter_long(strat, ctx)

    # Price drops to 33001 (-11 pts from entry at 33012, exceeds 10 pt SL)
    ts_sl = _TS_0930_UTC_NS + 40 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_sl, points=33_001))
    assert len(intents) == 1
    assert intents[0].side == Side.SELL
    assert intents[0].tif == TIF.IOC


def test_time_exit_triggers():
    """Should exit after max_hold_ns even if trailing stop not hit."""
    strat = _make_strategy(stop_loss_pts=100, trailing_stop_pts=100, max_hold_ns=60 * _ONE_SEC_NS)
    ctx = _make_ctx()
    _enter_long(strat, ctx)

    # 91s from start, entry was at 30s, so elapsed = 61s > 60s max hold
    ts_timeout = _TS_0930_UTC_NS + 91 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_timeout, points=33_014))
    assert len(intents) == 1
    assert intents[0].side == Side.SELL


def test_trailing_stop_not_triggered_when_peak_is_zero():
    """Trailing stop should not fire when peak PnL is zero (price never moved in our favor)."""
    strat = _make_strategy(stop_loss_pts=20, trailing_stop_pts=3, max_hold_ns=3600 * _ONE_SEC_NS)
    ctx = _make_ctx()
    _enter_long(strat, ctx)

    # Price drops 3 pts from entry (33012 -> 33009). trailing_stop_pts=3 but peak is 0.
    ts_dip = _TS_0930_UTC_NS + 40 * _ONE_SEC_NS
    intents = strat.handle_event(ctx, _make_stats(ts=ts_dip, points=33_009))
    assert len(intents) == 0


# ---------------------------------------------------------------------------
# Spread guard inherited from CBS
# ---------------------------------------------------------------------------


def test_spread_guard_blocks_entry_on_wide_spread():
    """Wide spread should block entry (inherited from CBS)."""
    strat = _make_strategy(max_spread_pts=3)
    ctx = _make_ctx()
    _seed_low_vol(strat, ctx)

    ts_trigger = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
    wide = _make_stats(ts=ts_trigger, points=33_012, spread_pts=5)
    intents = strat.handle_event(ctx, wide)
    assert len(intents) == 0


# ---------------------------------------------------------------------------
# Round-trip reset
# ---------------------------------------------------------------------------


def test_round_trip_resets_peak_pnl():
    """After completing a round trip, peak PnL should reset to 0."""
    strat = _make_strategy()
    ctx = _make_ctx()
    _enter_long(strat, ctx)

    exit_fill = _make_fill(Side.SELL, 33_020, order_id="exit-1")
    strat.handle_event(ctx, exit_fill)

    assert strat._state["TMFD6"] == "idle"
    assert strat._peak_pnl_scaled["TMFD6"] == 0


# ---------------------------------------------------------------------------
# P1-e regression: bad-type kwarg must surface at construction (not silently
# splat into super().__init__)
# ---------------------------------------------------------------------------


def test_bad_type_kwarg_raises_at_construction():
    """Passing wrong-type kwarg must raise TypeError at __init__ rather than
    silently splatting into CBS and crashing inside handle_event."""
    import pytest

    # `trigger_sigma` is typed `float` and CBS does float(trigger_sigma).
    # Passing an unparseable string must surface as TypeError/ValueError at
    # construction, not later inside handle_event.
    with pytest.raises((TypeError, ValueError)):
        MomentumBounceStrategy(
            strategy_id="momentum_bounce",
            trigger_sigma="not-a-number",  # type: ignore[arg-type]
        )


def test_explicit_kwargs_propagate_to_cbs():
    """Construction with all CBS-typed kwargs must succeed and propagate."""
    strat = MomentumBounceStrategy(
        strategy_id="momentum_bounce",
        trailing_stop_pts=7,
        stop_loss_pts=12,
        max_hold_ns=600 * _ONE_SEC_NS,
        lookback_ns=120 * _ONE_SEC_NS,
        trigger_sigma=2.5,
        take_profit_pts=4,
        min_vol_samples=10,
        max_spread_pts=2,
        session_start_sec=0,
        session_end_sec=86400,
    )
    assert strat._trailing_stop_pts == 7
    assert strat._stop_loss_pts == 12
    assert strat._max_hold_ns == 600 * _ONE_SEC_NS
    assert strat._lookback_ns == 120 * _ONE_SEC_NS
    assert strat._trigger_sigma == 2.5
    assert strat._take_profit_pts == 4
    assert strat._min_vol_samples == 10

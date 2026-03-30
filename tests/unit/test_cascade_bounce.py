"""Unit tests for the normalized CascadeBounceStrategy."""

from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus
from hft_platform.contracts.execution import Side as ExecSide
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.cascade_bounce import CascadeBounceStrategy
from hft_platform.strategy.base import StrategyContext

_ONE_SEC_NS = 1_000_000_000
_TS_0930_UTC_NS = 5400 * _ONE_SEC_NS
_TS_0900_UTC_NS = 3600 * _ONE_SEC_NS


def _mid_x2(points: int) -> int:
    return points * 20_000


def _scaled_price(points: int) -> int:
    return points * 10_000


def _make_stats(symbol: str = "TMFD6", ts: int = _TS_0930_UTC_NS, points: int = 33_000) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=_scaled_price(points) - 5_000,
        best_ask=_scaled_price(points) + 5_000,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=_mid_x2(points),
        spread_scaled=10_000,
    )


def _make_ctx(position: int = 0) -> StrategyContext:
    positions = {"TMFD6": position}
    next_intent_id = {"value": 0}

    def _intent_factory(**kwargs: object) -> OrderIntent:
        next_intent_id["value"] += 1
        return OrderIntent(
            intent_id=next_intent_id["value"],
            strategy_id="cascade_bounce",
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

    return StrategyContext(positions=positions, strategy_id="cascade_bounce", intent_factory=_intent_factory, price_scaler=_scale_price)


def _make_fill(side: Side, price_points: int, order_id: str = "entry-1") -> FillEvent:
    return FillEvent(
        fill_id=f"fill-{order_id}",
        account_id="acct",
        order_id=order_id,
        strategy_id="cascade_bounce",
        symbol="TMFD6",
        side=side,
        qty=1,
        price=_scaled_price(price_points),
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


def _make_order(status: OrderStatus, side: Side, order_id: str = "exit-1", price_points: int = 33_005) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        strategy_id="cascade_bounce",
        symbol="TMFD6",
        status=status,
        submitted_qty=1,
        filled_qty=0 if status != OrderStatus.FILLED else 1,
        remaining_qty=1 if status not in {OrderStatus.FILLED, OrderStatus.CANCELLED} else 0,
        price=_scaled_price(price_points),
        side=ExecSide.BUY if side == Side.BUY else ExecSide.SELL,
        ingest_ts_ns=0,
        broker_ts_ns=0,
    )


def _all_session_cbs(**overrides: object) -> CascadeBounceStrategy:
    defaults = {
        "symbols": ["TMFD6"],
        "session_start_sec": 0,
        "session_end_sec": 86400,
        "lookback_ns": 60 * _ONE_SEC_NS,
        "trigger_sigma": 3.0,
        "max_hold_ns": 300 * _ONE_SEC_NS,
        "stop_loss_pts": 6,
        "take_profit_pts": 8,
    }
    defaults.update(overrides)
    return CascadeBounceStrategy(**defaults)


def _seed_low_vol_history(cbs: CascadeBounceStrategy, ctx: StrategyContext, base_ts: int = _TS_0930_UTC_NS) -> None:
    for i in range(20):
        points = 33_000 + (1 if i % 2 else 0)
        cbs.handle_event(ctx, _make_stats(ts=base_ts + i * _ONE_SEC_NS, points=points))


class TestCBSInitialization:
    def test_default_params(self) -> None:
        cbs = CascadeBounceStrategy()
        assert cbs.strategy_id == "cascade_bounce"
        assert cbs._trigger_sigma == 3.0
        assert cbs._take_profit_pts == 8
        assert cbs._stop_loss_pts == 6

    def test_session_defaults_remain_supported(self) -> None:
        cbs = CascadeBounceStrategy()
        assert cbs._session_start_sec == 33300
        assert cbs._session_end_sec == 48900

    def test_strategy_registry_uses_normalized_tmfd6_params(self) -> None:
        cfg = yaml.safe_load(Path("config/base/strategies.yaml").read_text(encoding="utf-8"))
        enabled = {item["id"]: item["enabled"] for item in cfg["strategies"]}
        cbs = next(item for item in cfg["strategies"] if item["id"] == "CBS_TMFD6")
        params = cbs["params"]

        assert enabled["OPPORTUNISTIC_MM_TMFD6"] is False
        assert enabled["CBS_TMFD6"] is True
        assert params["lookback_ns"] > 0
        assert params["trigger_sigma"] > 0
        assert params["take_profit_pts"] > 0
        assert params["stop_loss_pts"] > 0
        assert params["session_start_sec"] == 33300  # 09:15 TWN fallback gate
        assert params["session_end_sec"] == 48900    # 13:35 TWN fallback gate


class TestCBSSessionGate:
    def test_no_entry_before_session_start(self) -> None:
        cbs = CascadeBounceStrategy(symbols=["TMFD6"])
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx, base_ts=_TS_0900_UTC_NS)

        intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0900_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))

        assert intents == []

    def test_entry_after_session_start(self) -> None:
        cbs = _all_session_cbs(session_start_sec=33300, session_end_sec=48900)
        ctx = _make_ctx()
        start_ts = _TS_0930_UTC_NS
        _seed_low_vol_history(cbs, ctx, base_ts=start_ts)

        intents = cbs.handle_event(ctx, _make_stats(ts=start_ts + 30 * _ONE_SEC_NS, points=32_990))

        assert len(intents) == 1


class TestCBSNormalizedEntry:
    def test_entry_uses_sigma_over_local_vol(self) -> None:
        cbs = _all_session_cbs(trigger_sigma=3.0)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)

        intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))

        assert len(intents) == 1
        assert intents[0].side == Side.BUY
        assert intents[0].tif == TIF.IOC
        assert cbs._state["TMFD6"] == "awaiting_entry_fill"

    def test_large_rise_triggers_short_ioc_entry(self) -> None:
        cbs = _all_session_cbs(trigger_sigma=3.0)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)

        intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=33_012))

        assert len(intents) == 1
        assert intents[0].side == Side.SELL
        assert intents[0].tif == TIF.IOC

    def test_small_move_does_not_trigger(self) -> None:
        cbs = _all_session_cbs(trigger_sigma=3.0)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)

        intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_999))

        assert intents == []


class TestCBSExitStateMachine:
    def test_entry_fill_places_passive_take_profit(self) -> None:
        cbs = _all_session_cbs()
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))
        ctx.positions["TMFD6"] = 1

        intents = cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))

        assert len(intents) == 1
        assert intents[0].intent_type == IntentType.NEW
        assert intents[0].tif == TIF.LIMIT
        assert intents[0].side == Side.SELL
        assert intents[0].price == _scaled_price(32_999)
        assert cbs._state["TMFD6"] == "exit_live"

    def test_on_order_tracks_and_clears_exit_order(self) -> None:
        cbs = _all_session_cbs()
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))
        ctx.positions["TMFD6"] = 1
        cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))

        cbs.handle_event(ctx, _make_order(OrderStatus.SUBMITTED, Side.SELL, order_id="exit-1", price_points=32_999))
        assert cbs._exit_order_id["TMFD6"] == "exit-1"

        cbs.handle_event(ctx, _make_order(OrderStatus.CANCELLED, Side.SELL, order_id="exit-1", price_points=32_999))
        assert cbs._exit_order_id["TMFD6"] == ""
        assert cbs._state["TMFD6"] == "positioned"

    def test_stop_loss_cancels_resting_exit_then_uses_ioc(self) -> None:
        cbs = _all_session_cbs(stop_loss_pts=4)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))
        ctx.positions["TMFD6"] = 1
        cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))
        cbs.handle_event(ctx, _make_order(OrderStatus.SUBMITTED, Side.SELL, order_id="exit-1", price_points=32_999))

        cancel_intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 40 * _ONE_SEC_NS, points=32_986))
        assert len(cancel_intents) == 1
        assert cancel_intents[0].intent_type == IntentType.CANCEL

        cbs.handle_event(ctx, _make_order(OrderStatus.CANCELLED, Side.SELL, order_id="exit-1", price_points=32_999))
        close_intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 41 * _ONE_SEC_NS, points=32_986))
        assert len(close_intents) == 1
        assert close_intents[0].intent_type == IntentType.NEW
        assert close_intents[0].tif == TIF.IOC
        assert close_intents[0].side == Side.SELL

    def test_aggressive_exit_ioc_rejection_allows_retry(self) -> None:
        # Simulate: position entered, stop-loss triggers, aggressive IOC fired, IOC comes back CANCELLED
        cbs = _all_session_cbs(stop_loss_pts=4)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))
        ctx.positions["TMFD6"] = 1
        cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))
        # Skip resting limit so no exit_order_id to cancel
        cbs._awaiting_exit_order["TMFD6"] = False
        cbs._exit_order_id["TMFD6"] = ""
        cbs._state["TMFD6"] = "positioned"

        # Stop-loss triggers aggressive exit
        close_intents = cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 40 * _ONE_SEC_NS, points=32_986))
        assert len(close_intents) == 1
        assert close_intents[0].tif == TIF.IOC
        assert cbs._aggressive_exit_inflight["TMFD6"] is True
        assert cbs._awaiting_exit_order["TMFD6"] is True

        # IOC comes back CANCELLED with no fill
        cancel_event = _make_order(OrderStatus.CANCELLED, Side.SELL, order_id="ioc-exit-1", price_points=32_986)
        cbs.handle_event(ctx, cancel_event)

        # Flag must be reset so retry is possible on next tick
        assert cbs._aggressive_exit_inflight["TMFD6"] is False
        assert cbs._awaiting_exit_order["TMFD6"] is False
        assert cbs._pending_force_close["TMFD6"] is True

    def test_aggressive_exit_ioc_filled_completes_trip(self) -> None:
        # Simulate: aggressive IOC fired, fill arrives — round trip completes normally
        cbs = _all_session_cbs(stop_loss_pts=4)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 30 * _ONE_SEC_NS, points=32_990))
        ctx.positions["TMFD6"] = 1
        cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))
        cbs._awaiting_exit_order["TMFD6"] = False
        cbs._exit_order_id["TMFD6"] = ""
        cbs._state["TMFD6"] = "positioned"

        # Stop-loss triggers aggressive exit
        cbs.handle_event(ctx, _make_stats(ts=_TS_0930_UTC_NS + 40 * _ONE_SEC_NS, points=32_986))
        assert cbs._aggressive_exit_inflight["TMFD6"] is True

        # IOC fills → on_fill exits with exit_side → _complete_round_trip
        ctx.positions["TMFD6"] = 0
        cbs.handle_event(ctx, _make_fill(Side.SELL, 32_986, order_id="ioc-exit-fill"))

        assert cbs._state["TMFD6"] == "idle"
        assert cbs._direction["TMFD6"] == 0
        assert cbs._aggressive_exit_inflight["TMFD6"] is False

    def test_close_fill_resets_state_and_cooldown(self) -> None:
        cbs = _all_session_cbs(max_hold_ns=120 * _ONE_SEC_NS)
        ctx = _make_ctx()
        _seed_low_vol_history(cbs, ctx)
        entry_ts = _TS_0930_UTC_NS + 30 * _ONE_SEC_NS
        cbs.handle_event(ctx, _make_stats(ts=entry_ts, points=32_990))
        ctx.positions["TMFD6"] = 1
        cbs.handle_event(ctx, _make_fill(Side.BUY, 32_991))

        cbs.handle_event(ctx, _make_fill(Side.SELL, 32_999, order_id="exit-fill"))

        assert cbs._state["TMFD6"] == "idle"
        assert cbs._direction["TMFD6"] == 0
        assert cbs._next_allowed_ts["TMFD6"] == entry_ts + 120 * _ONE_SEC_NS

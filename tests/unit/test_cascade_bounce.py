"""Unit tests for CascadeBounceStrategy (CBS-40-300)."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.cascade_bounce import CascadeBounceStrategy

# Helper: 09:30 TST as UTC epoch ns (09:30 TST = 01:30 UTC = 5400s into UTC day)
# With utc_offset_sec=28800, sec_of_day = 5400 + 28800 = 34200 → 09:30 local
_TS_0930_UTC_NS = 5400 * 1_000_000_000  # 01:30 UTC = 09:30 TST
_TS_0900_UTC_NS = 3600 * 1_000_000_000  # 01:00 UTC = 09:00 TST
_TS_1340_UTC_NS = (5 * 3600 + 40 * 60) * 1_000_000_000  # 05:40 UTC = 13:40 TST
_TS_1330_UTC_NS = (5 * 3600 + 30 * 60) * 1_000_000_000  # 05:30 UTC = 13:30 TST
_ONE_SEC_NS = 1_000_000_000


def _make_stats(
    symbol: str = "TMFD6",
    ts: int = _TS_0930_UTC_NS,
    mid_x2: int = 660_000_000,  # 33000 * 10000 * 2
    spread_scaled: int = 10_000,  # 1 point
    best_bid: int = 329_995_000,
    best_ask: int = 330_005_000,
    imbalance: float = 0.0,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=mid_x2,
        spread_scaled=spread_scaled,
    )


def _make_ctx(position: int = 0) -> MagicMock:
    ctx = MagicMock()
    ctx.positions = {"TMFD6": position}
    ctx.place_order = MagicMock(return_value=MagicMock())
    ctx.get_l1_scaled = MagicMock(return_value=(0, 329_995_000, 330_005_000, 660_000_000, 10_000, 10, 10))
    return ctx


def _all_session_cbs(**overrides: object) -> CascadeBounceStrategy:
    """CBS with session gate fully open (for non-session tests)."""
    defaults = {
        "symbols": ["TMFD6"],
        "session_start_sec": 0,
        "session_end_sec": 86400,
    }
    defaults.update(overrides)
    return CascadeBounceStrategy(**defaults)


class TestCBSInitialization:
    def test_default_params(self) -> None:
        cbs = CascadeBounceStrategy()
        assert cbs.strategy_id == "cascade_bounce"
        assert cbs._move_threshold_bps == 40
        assert cbs._hold_ns == 300_000_000_000
        assert cbs._stop_loss_bps == 15

    def test_custom_params(self) -> None:
        cbs = CascadeBounceStrategy(
            strategy_id="cbs_test",
            move_threshold_bps=50,
            hold_ns=600_000_000_000,
            stop_loss_bps=20,
        )
        assert cbs._move_threshold_bps == 50
        assert cbs._hold_ns == 600_000_000_000
        assert cbs._stop_loss_bps == 20

    def test_session_params_default(self) -> None:
        cbs = CascadeBounceStrategy()
        assert cbs._session_start_sec == 33300  # 09:15
        assert cbs._session_end_sec == 48900  # 13:35
        assert cbs._utc_offset_sec == 28800  # UTC+8

    def test_session_params_custom(self) -> None:
        cbs = CascadeBounceStrategy(session_start_sec=36000, session_end_sec=46800)
        assert cbs._session_start_sec == 36000  # 10:00
        assert cbs._session_end_sec == 46800  # 13:00


class TestCBSSessionGate:
    def test_no_entry_before_session_start(self) -> None:
        """CBS should not enter before 09:15 TST (opening momentum regime)."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        # 09:00 TST = before session start (09:15)
        base_ts = _TS_0900_UTC_NS
        base_mid = 660_000_000

        # Build price history within pre-session window
        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps drop at 09:01:40 TST (still before 09:15)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(ctx, _make_stats(ts=ts_drop, mid_x2=drop_mid))

        assert len(intents) == 0

    def test_entry_after_session_start(self) -> None:
        """CBS should enter after 09:15 TST."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        # Start at 09:20 TST (in session)
        base_ts = _TS_0930_UTC_NS - 10 * 60 * _ONE_SEC_NS  # 09:20 TST
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Large drop after 09:21:40 TST (in session)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

        assert len(intents) == 1

    def test_no_entry_after_session_end(self) -> None:
        """CBS should not enter after 13:35 TST."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        # 13:40 TST = after session end (13:35)
        base_ts = _TS_1340_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(ctx, _make_stats(ts=ts_drop, mid_x2=drop_mid))

        assert len(intents) == 0

    def test_entry_just_before_session_end(self) -> None:
        """CBS should enter at 13:30 TST (within session)."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        # 13:30 TST = within session (before 13:35 end)
        base_ts = _TS_1330_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

        assert len(intents) == 1

    def test_session_boundary_exact_start(self) -> None:
        """Exactly at 09:15:00 TST should be in-session."""
        cbs = CascadeBounceStrategy(symbols=["TMFD6"])
        # 09:15 TST = 01:15 UTC = 4500s epoch
        ts_at_boundary = 4500 * _ONE_SEC_NS
        assert cbs._in_session(ts_at_boundary) is True

    def test_session_boundary_one_sec_before_start(self) -> None:
        """One second before 09:15 TST should be out of session."""
        cbs = CascadeBounceStrategy(symbols=["TMFD6"])
        ts_before = 4499 * _ONE_SEC_NS  # 01:14:59 UTC = 09:14:59 TST
        assert cbs._in_session(ts_before) is False

    def test_session_boundary_exact_end(self) -> None:
        """Exactly at 13:35:00 TST should be in-session."""
        cbs = CascadeBounceStrategy(symbols=["TMFD6"])
        # 13:35 TST = 05:35 UTC = 20100s epoch
        ts_at_end = 20100 * _ONE_SEC_NS
        assert cbs._in_session(ts_at_end) is True

    def test_session_boundary_one_sec_after_end(self) -> None:
        """One second after 13:35 TST should be out of session."""
        cbs = CascadeBounceStrategy(symbols=["TMFD6"])
        ts_after = 20101 * _ONE_SEC_NS  # 05:35:01 UTC = 13:35:01 TST
        assert cbs._in_session(ts_after) is False

    def test_custom_utc_offset(self) -> None:
        """Verify session gate works with different UTC offsets."""
        # UTC+0 timezone: 09:15 local = 09:15 UTC = 33300s epoch
        cbs = CascadeBounceStrategy(
            symbols=["TMFD6"],
            utc_offset_sec=0,
            session_start_sec=33300,
            session_end_sec=48900,
        )
        ts_in = 33300 * _ONE_SEC_NS
        ts_before = 33299 * _ONE_SEC_NS
        assert cbs._in_session(ts_in) is True
        assert cbs._in_session(ts_before) is False


class TestCBSMoveDetection:
    def test_no_entry_on_small_move(self) -> None:
        """Moves below threshold should not trigger entry."""
        cbs = _all_session_cbs(move_threshold_bps=40)
        ctx = _make_ctx()

        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 20 bps move (below 40 bps threshold)
        small_drop_mid = int(base_mid * (1 - 20 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(ctx, _make_stats(ts=ts_drop, mid_x2=small_drop_mid))

        assert len(intents) == 0

    def test_entry_on_large_move(self) -> None:
        """Moves above threshold should trigger contrarian entry."""
        cbs = _all_session_cbs(move_threshold_bps=40)
        ctx = _make_ctx()

        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps drop -> should trigger buy (contrarian)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

        assert len(intents) == 1
        assert cbs._direction["TMFD6"] == 1  # +1 = long
        assert cbs._state["TMFD6"] == "positioned"

    def test_contrarian_direction_on_rise(self) -> None:
        """After a large up-move, should sell (contrarian)."""
        cbs = _all_session_cbs(move_threshold_bps=40)
        ctx = _make_ctx()

        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps rise -> should trigger sell
        rise_mid = int(base_mid * (1 + 50 / 10000))
        ts_rise = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_rise,
                mid_x2=rise_mid,
                best_bid=rise_mid // 2 - 5000,
                best_ask=rise_mid // 2 + 5000,
            ),
        )

        assert len(intents) == 1
        assert cbs._direction["TMFD6"] == -1  # -1 = short
        assert cbs._state["TMFD6"] == "positioned"


class TestCBSExitLogic:
    def _enter_position(self, cbs: CascadeBounceStrategy, ctx: MagicMock) -> None:
        """Helper: build price history and trigger entry on a 50 bps drop."""
        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

    def test_time_exit(self) -> None:
        """Position should be closed after hold period."""
        cbs = _all_session_cbs(move_threshold_bps=40, hold_ns=300_000_000_000)
        ctx = _make_ctx()
        self._enter_position(cbs, ctx)

        assert cbs._state["TMFD6"] == "positioned"

        # Advance past hold period
        base_ts = _TS_0930_UTC_NS
        exit_ts = base_ts + 100 * _ONE_SEC_NS + 301_000_000_000  # entry + 301s
        exit_mid = int(660_000_000 * (1 - 45 / 10000))  # still down

        intents = cbs.handle_event(ctx, _make_stats(ts=exit_ts, mid_x2=exit_mid))

        assert len(intents) == 1
        assert cbs._state["TMFD6"] == "idle"

    def test_stop_loss_exit(self) -> None:
        """Position should be closed when adverse move exceeds stop-loss."""
        cbs = _all_session_cbs(move_threshold_bps=40, stop_loss_bps=15)
        ctx = _make_ctx()
        self._enter_position(cbs, ctx)

        # We entered long after a drop. Further drop = adverse for our long.
        entry_mid = cbs._entry_mid_x2["TMFD6"]
        adverse_mid = int(entry_mid * (1 - 20 / 10000))
        base_ts = _TS_0930_UTC_NS
        ts = base_ts + 110 * _ONE_SEC_NS  # 10s after entry

        intents = cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=adverse_mid))

        assert len(intents) == 1
        assert cbs._state["TMFD6"] == "idle"


class TestCBSNonOverlapping:
    def test_no_reentry_during_cooldown(self) -> None:
        """After exit, no new entry until entry_ts + hold_ns."""
        cbs = _all_session_cbs(
            move_threshold_bps=40,
            hold_ns=300_000_000_000,
            stop_loss_bps=15,
        )
        ctx = _make_ctx()

        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        # Build history and enter
        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Trigger entry at t=100s
        drop_mid = int(base_mid * (1 - 50 / 10000))
        entry_ts = base_ts + 100 * _ONE_SEC_NS
        cbs.handle_event(
            ctx,
            _make_stats(
                ts=entry_ts,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )
        assert cbs._state["TMFD6"] == "positioned"

        # Stop-loss at t=110s (10s after entry)
        adverse_mid = int(drop_mid * (1 - 20 / 10000))
        stop_ts = base_ts + 110 * _ONE_SEC_NS
        cbs.handle_event(ctx, _make_stats(ts=stop_ts, mid_x2=adverse_mid))
        assert cbs._state["TMFD6"] == "idle"

        # next_allowed should be entry_ts + hold_ns, not exit_ts + cooldown
        expected_next = entry_ts + 300_000_000_000
        assert cbs._next_allowed_ts["TMFD6"] == expected_next

        # Try to enter again at t=200s (within cooldown, entry+100s < entry+300s)
        reentry_ts = base_ts + 200 * _ONE_SEC_NS
        for i in range(111, 200):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=adverse_mid))

        another_drop = int(adverse_mid * (1 - 50 / 10000))
        intents = cbs.handle_event(ctx, _make_stats(ts=reentry_ts, mid_x2=another_drop))

        assert cbs._state["TMFD6"] == "idle"
        assert len(intents) == 0


class TestCBSExecutionOptimizer:
    """Tests for ExecutionOptimizer integration into CBS."""

    def test_optimizer_disabled_by_default(self) -> None:
        """Default CBS uses market orders (optimizer disabled)."""
        cbs = _all_session_cbs(move_threshold_bps=40)
        assert cbs._exec_optimizer.enabled is False

    def test_optimizer_enabled_uses_limit_on_wide_spread(self) -> None:
        """With optimizer enabled and wide spread, should use limit order."""
        cbs = _all_session_cbs(
            move_threshold_bps=40,
            exec_optimizer_enabled=True,
            exec_spread_threshold_pts=2,
            exec_fill_score_threshold=1.0,
        )
        ctx = _make_ctx()
        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps drop with wide spread (3 pts = 30000 scaled) + favorable depth
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                spread_scaled=30_000,  # 3 pts
                best_bid=drop_mid // 2 - 15000,
                best_ask=drop_mid // 2 + 15000,
            ),
        )

        assert len(intents) == 1
        # With optimizer enabled + wide spread, enters pending_limit
        assert cbs._state["TMFD6"] == "pending_limit"

    def test_optimizer_enabled_narrow_spread_uses_market(self) -> None:
        """With optimizer enabled but narrow spread, should use market order."""
        cbs = _all_session_cbs(
            move_threshold_bps=40,
            exec_optimizer_enabled=True,
            exec_spread_threshold_pts=2,
            exec_fill_score_threshold=1.0,
        )
        ctx = _make_ctx()
        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps drop with narrow spread (1 pt = 10000 scaled)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                spread_scaled=10_000,  # 1 pt, below threshold
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

        assert len(intents) == 1
        # Narrow spread → market order → positioned directly
        assert cbs._state["TMFD6"] == "positioned"

    def test_pending_limit_transitions_to_positioned_on_fill(self) -> None:
        """When pending limit fills, state transitions to positioned."""
        cbs = _all_session_cbs(
            move_threshold_bps=40,
            exec_optimizer_enabled=True,
            exec_spread_threshold_pts=2,
            exec_fill_score_threshold=1.0,
        )
        ctx = _make_ctx()
        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Enter pending_limit (wide spread)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop, mid_x2=drop_mid,
                spread_scaled=30_000,
                best_bid=drop_mid // 2 - 15000,
                best_ask=drop_mid // 2 + 15000,
            ),
        )
        assert cbs._state["TMFD6"] == "pending_limit"

        # Simulate fill — position becomes +1
        ctx.positions["TMFD6"] = 1
        ts_fill = ts_drop + _ONE_SEC_NS
        cbs.handle_event(ctx, _make_stats(ts=ts_fill, mid_x2=drop_mid))

        assert cbs._state["TMFD6"] == "positioned"

    def test_pending_limit_timeout_falls_back_to_market(self) -> None:
        """After timeout, pending limit should cancel and use market order."""
        cbs = _all_session_cbs(
            move_threshold_bps=40,
            exec_optimizer_enabled=True,
            exec_spread_threshold_pts=2,
            exec_fill_score_threshold=1.0,
            exec_limit_timeout_ns=2_000_000_000,  # 2s timeout
        )
        ctx = _make_ctx()
        base_ts = _TS_0930_UTC_NS
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * _ONE_SEC_NS
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Enter pending_limit
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * _ONE_SEC_NS
        cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop, mid_x2=drop_mid,
                spread_scaled=30_000,
                best_bid=drop_mid // 2 - 15000,
                best_ask=drop_mid // 2 + 15000,
            ),
        )
        assert cbs._state["TMFD6"] == "pending_limit"

        # Position NOT filled (still 0), timeout elapsed (3s > 2s timeout)
        ts_timeout = ts_drop + 3 * _ONE_SEC_NS
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_timeout, mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 15000,
                best_ask=drop_mid // 2 + 15000,
            ),
        )

        # Should have placed market fallback order
        assert len(intents) == 1
        assert cbs._state["TMFD6"] == "positioned"

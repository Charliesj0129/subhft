"""Unit tests for CascadeBounceStrategy (CBS-40-300)."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.cascade_bounce import CascadeBounceStrategy


def _make_stats(
    symbol: str = "TMFD6",
    ts: int = 1_000_000_000_000,
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


class TestCBSSessionGate:
    def test_no_entry_during_opening(self) -> None:
        """CBS should not enter during the first 30 minutes."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,  # low threshold for testing
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        # Feed initial prices for 10 minutes (within opening gate)
        for i in range(100):
            ts = base_ts + i * 1_000_000_000  # 1s apart
            intents = cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Feed a 50 bps drop (within opening gate, 100s in)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * 1_000_000_000  # 100s = within 30 min
        intents = cbs.handle_event(ctx, _make_stats(ts=ts_drop, mid_x2=drop_mid))

        # Should NOT enter (still in opening 30 min)
        assert len(intents) == 0

    def test_entry_after_opening(self) -> None:
        """CBS should enter after the 30-minute opening gate."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=10,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        # Feed prices for 31 minutes (past opening gate)
        for i in range(0, 1900, 10):  # every 10s for ~31 min
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Now feed a large drop (after opening)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 1900 * 1_000_000_000
        intents = cbs.handle_event(
            ctx,
            _make_stats(
                ts=ts_drop,
                mid_x2=drop_mid,
                best_bid=drop_mid // 2 - 5000,
                best_ask=drop_mid // 2 + 5000,
            ),
        )

        # Should enter contrarian (buy after drop)
        assert len(intents) == 1


class TestCBSMoveDetection:
    def test_no_entry_on_small_move(self) -> None:
        """Moves below threshold should not trigger entry."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            session_start_offset_ns=0,  # disable opening gate for this test
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        # Build price history
        for i in range(100):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 20 bps move (below 40 bps threshold)
        small_drop_mid = int(base_mid * (1 - 20 / 10000))
        ts_drop = base_ts + 100 * 1_000_000_000
        intents = cbs.handle_event(ctx, _make_stats(ts=ts_drop, mid_x2=small_drop_mid))

        assert len(intents) == 0

    def test_entry_on_large_move(self) -> None:
        """Moves above threshold should trigger contrarian entry."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            session_start_offset_ns=0,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps drop → should trigger buy (contrarian)
        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * 1_000_000_000
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
        # Contrarian after drop = buy (direction=long)
        assert cbs._direction["TMFD6"] == 1  # +1 = long
        assert cbs._state["TMFD6"] == "positioned"

    def test_contrarian_direction_on_rise(self) -> None:
        """After a large up-move, should sell (contrarian)."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            session_start_offset_ns=0,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # 50 bps rise → should trigger sell
        rise_mid = int(base_mid * (1 + 50 / 10000))
        ts_rise = base_ts + 100 * 1_000_000_000
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
        # Contrarian after rise = sell (direction=short)
        assert cbs._direction["TMFD6"] == -1  # -1 = short
        assert cbs._state["TMFD6"] == "positioned"


class TestCBSExitLogic:
    def _enter_position(self, cbs: CascadeBounceStrategy, ctx: MagicMock) -> None:
        """Helper: build price history and trigger entry on a 50 bps drop."""
        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        for i in range(100):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        drop_mid = int(base_mid * (1 - 50 / 10000))
        ts_drop = base_ts + 100 * 1_000_000_000
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
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            hold_ns=300_000_000_000,
            session_start_offset_ns=0,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()
        self._enter_position(cbs, ctx)

        assert cbs._state["TMFD6"] == "positioned"

        # Advance past hold period
        base_ts = 1_000_000_000_000
        exit_ts = base_ts + 100 * 1_000_000_000 + 301_000_000_000  # entry + 301s
        exit_mid = int(660_000_000 * (1 - 45 / 10000))  # still down

        intents = cbs.handle_event(ctx, _make_stats(ts=exit_ts, mid_x2=exit_mid))

        # Should emit exit order
        assert len(intents) == 1
        assert cbs._state["TMFD6"] == "idle"

    def test_stop_loss_exit(self) -> None:
        """Position should be closed when adverse move exceeds stop-loss."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            stop_loss_bps=15,
            session_start_offset_ns=0,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()
        self._enter_position(cbs, ctx)

        # We entered long after a drop. Further drop = adverse for our long.
        entry_mid = cbs._entry_mid_x2["TMFD6"]
        # 20 bps further drop from entry (exceeds 15 bps stop)
        adverse_mid = int(entry_mid * (1 - 20 / 10000))
        base_ts = 1_000_000_000_000
        ts = base_ts + 110 * 1_000_000_000  # 10s after entry

        intents = cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=adverse_mid))

        assert len(intents) == 1
        assert cbs._state["TMFD6"] == "idle"


class TestCBSNonOverlapping:
    def test_no_reentry_during_cooldown(self) -> None:
        """After exit, no new entry until entry_ts + hold_ns."""
        cbs = CascadeBounceStrategy(
            move_threshold_bps=40,
            hold_ns=300_000_000_000,
            stop_loss_bps=15,
            session_start_offset_ns=0,
            symbols=["TMFD6"],
        )
        ctx = _make_ctx()

        base_ts = 1_000_000_000_000
        base_mid = 660_000_000

        # Build history and enter
        for i in range(100):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=base_mid))

        # Trigger entry at t=100s
        drop_mid = int(base_mid * (1 - 50 / 10000))
        entry_ts = base_ts + 100 * 1_000_000_000
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
        stop_ts = base_ts + 110 * 1_000_000_000
        cbs.handle_event(ctx, _make_stats(ts=stop_ts, mid_x2=adverse_mid))
        assert cbs._state["TMFD6"] == "idle"

        # next_allowed should be entry_ts + hold_ns, not exit_ts + cooldown
        expected_next = entry_ts + 300_000_000_000
        assert cbs._next_allowed_ts["TMFD6"] == expected_next

        # Try to enter again at t=200s (within cooldown, entry+100s < entry+300s)
        reentry_ts = base_ts + 200 * 1_000_000_000
        # Feed prices leading to another large drop
        for i in range(111, 200):
            ts = base_ts + i * 1_000_000_000
            cbs.handle_event(ctx, _make_stats(ts=ts, mid_x2=adverse_mid))

        another_drop = int(adverse_mid * (1 - 50 / 10000))
        intents = cbs.handle_event(ctx, _make_stats(ts=reentry_ts, mid_x2=another_drop))

        # Should NOT enter (within non-overlapping cooldown)
        assert cbs._state["TMFD6"] == "idle"
        assert len(intents) == 0

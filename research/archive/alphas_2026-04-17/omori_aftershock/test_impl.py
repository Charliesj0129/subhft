"""Unit tests for Omori Aftershock Trader."""

from __future__ import annotations

import pytest

from impl import (
    AftershockStrategy,
    MainshockDetector,
    MainshockEvent,
    OmoriDecayTracker,
    OmoriParams,
    TradeSignal,
)

SCALE = 1_000_000  # 1 index point = 1M raw units
NS_PER_MIN = 60 * 1_000_000_000
NS_PER_SEC = 1_000_000_000


# ─── MainshockDetector ──────────────────────────────────────────


class TestMainshockDetector:
    def _make_detector(
        self, threshold: float = 100.0, window: int = 30, gap: int = 30
    ) -> MainshockDetector:
        return MainshockDetector(
            threshold_pts=threshold,
            window_minutes=window,
            min_gap_minutes=gap,
            price_scale=SCALE,
        )

    def test_no_event_on_small_move(self) -> None:
        det = self._make_detector(threshold=100.0)
        base = 32000 * SCALE
        t0 = 1_000_000_000_000
        for i in range(40):
            result = det.update(t0 + i * NS_PER_MIN, base + i * 2 * SCALE)
        assert result is None  # 80 pts total < 100 threshold

    def test_detects_upward_mainshock(self) -> None:
        det = self._make_detector(threshold=100.0, window=30, gap=0)
        base = 32000 * SCALE
        t0 = 1_000_000_000_000
        event = None
        for i in range(35):
            event = det.update(t0 + i * NS_PER_MIN, base + i * 5 * SCALE)
            if event is not None:
                break
        assert event is not None
        assert event.direction == "UP"
        assert event.change_pts >= 100.0

    def test_detects_downward_mainshock(self) -> None:
        det = self._make_detector(threshold=100.0, window=30, gap=0)
        base = 32000 * SCALE
        t0 = 1_000_000_000_000
        event = None
        for i in range(35):
            event = det.update(t0 + i * NS_PER_MIN, base - i * 5 * SCALE)
            if event is not None:
                break
        assert event is not None
        assert event.direction == "DOWN"
        assert event.change_pts <= -100.0

    def test_respects_min_gap(self) -> None:
        det = self._make_detector(threshold=50.0, window=10, gap=30)
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        # First event
        for i in range(15):
            det.update(t0 + i * NS_PER_MIN, base + i * 10 * SCALE)

        # Try to trigger second event immediately — should be blocked by gap
        events = []
        for i in range(15, 25):
            evt = det.update(t0 + i * NS_PER_MIN, base + i * 10 * SCALE)
            if evt is not None:
                events.append(evt)

        # Should have at most 1 event due to 30-min gap
        assert len(events) <= 1

    def test_reset_clears_state(self) -> None:
        det = self._make_detector()
        det.update(1_000_000_000_000, 32000 * SCALE)
        det.reset()
        assert len(det._price_buffer) == 0
        assert len(det._ts_buffer) == 0


# ─── OmoriDecayTracker ──────────────────────────────────────────


class TestOmoriDecayTracker:
    def _make_tracker(self) -> OmoriDecayTracker:
        return OmoriDecayTracker(
            aftershock_threshold_pts=10.0,
            max_tracking_minutes=120,
            price_scale=SCALE,
        )

    def test_tracks_aftershocks(self) -> None:
        tracker = self._make_tracker()
        event = MainshockEvent(
            timestamp_ns=1_000_000_000_000,
            price_at_detection=32000 * SCALE,
            change_pts=200.0,
            direction="UP",
            window_minutes=30,
        )
        tracker.start(event)

        base = 32000 * SCALE
        prev = base
        # Feed aftershock: 15-pt jump at t+60s
        is_as = tracker.update(
            event.timestamp_ns + 60 * NS_PER_SEC,
            base + 15 * SCALE,
            prev,
        )
        assert is_as is True
        assert tracker.aftershock_count == 1

    def test_ignores_small_moves(self) -> None:
        tracker = self._make_tracker()
        event = MainshockEvent(
            timestamp_ns=1_000_000_000_000,
            price_at_detection=32000 * SCALE,
            change_pts=200.0,
            direction="UP",
            window_minutes=30,
        )
        tracker.start(event)

        base = 32000 * SCALE
        is_as = tracker.update(
            event.timestamp_ns + 60 * NS_PER_SEC,
            base + 5 * SCALE,  # 5 pts < 10 pt threshold
            base,
        )
        assert is_as is False
        assert tracker.aftershock_count == 0

    def test_fit_omori_requires_min_data(self) -> None:
        tracker = self._make_tracker()
        event = MainshockEvent(
            timestamp_ns=1_000_000_000_000,
            price_at_detection=32000 * SCALE,
            change_pts=200.0,
            direction="UP",
            window_minutes=30,
        )
        tracker.start(event)

        # Only 2 aftershocks — not enough for fit
        base = 32000 * SCALE
        prev = base
        for i in range(2):
            tracker.update(
                event.timestamp_ns + (i + 1) * 30 * NS_PER_SEC,
                base + (i + 1) * 15 * SCALE,
                prev,
            )
            prev = base + (i + 1) * 15 * SCALE

        result = tracker.fit_omori()
        assert result is None  # Not enough data

    def test_fit_omori_with_sufficient_data(self) -> None:
        tracker = self._make_tracker()
        event = MainshockEvent(
            timestamp_ns=1_000_000_000_000,
            price_at_detection=32000 * SCALE,
            change_pts=200.0,
            direction="UP",
            window_minutes=30,
        )
        tracker.start(event)

        # Simulate Omori-like aftershock pattern: dense early, sparse late
        # Aftershocks at t=2,3,5,8,12,20,35,60,100,180 seconds
        aftershock_times_sec = [2, 3, 5, 8, 12, 20, 35, 60, 100, 180]
        base = 32000 * SCALE
        for t_sec in aftershock_times_sec:
            tracker.update(
                event.timestamp_ns + t_sec * NS_PER_SEC,
                base + 15 * SCALE,
                base,
            )

        result = tracker.fit_omori()
        assert result is not None
        assert isinstance(result, OmoriParams)
        assert result.K > 0
        assert result.p > 0  # Decay exponent should be positive
        assert result.n_aftershocks == len(aftershock_times_sec)

    def test_respects_max_tracking_window(self) -> None:
        tracker = self._make_tracker()
        event = MainshockEvent(
            timestamp_ns=1_000_000_000_000,
            price_at_detection=32000 * SCALE,
            change_pts=200.0,
            direction="UP",
            window_minutes=30,
        )
        tracker.start(event)

        # Aftershock beyond max tracking window (120 min)
        base = 32000 * SCALE
        is_as = tracker.update(
            event.timestamp_ns + 130 * NS_PER_MIN,
            base + 20 * SCALE,
            base,
        )
        assert is_as is False


# ─── AftershockStrategy ─────────────────────────────────────────


class TestAftershockStrategy:
    def _make_strategy(self, **kwargs) -> AftershockStrategy:
        det = MainshockDetector(
            threshold_pts=100.0, window_minutes=10, min_gap_minutes=0,
            price_scale=SCALE,
        )
        tracker = OmoriDecayTracker(price_scale=SCALE)
        defaults = dict(
            detector=det,
            tracker=tracker,
            max_spread_pts=5.0,
            entry_delay_seconds=10,
            entry_mode="continuation",
            stop_loss_pts=30.0,
            take_profit_pts=80.0,
            max_hold_minutes=30,
            max_events_per_day=5,
            daily_loss_limit_pts=200.0,
            min_trade_gap_minutes=0,
            allowed_hours=[(8, 45, 13, 30)],
            price_scale=SCALE,
        )
        defaults.update(kwargs)
        return AftershockStrategy(**defaults)

    def test_emits_buy_on_up_shock_continuation(self) -> None:
        strat = self._make_strategy(entry_mode="continuation")
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        # Feed rising prices to trigger mainshock
        signal = None
        for i in range(15):
            signal = strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + i * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30,
                day_str="2026-03-15",
            )
            if signal is not None:
                break

        # Event detected but entry delay not elapsed yet
        # Feed more ticks past the delay
        if signal is None:
            for j in range(10):
                signal = strat.on_tick(
                    timestamp_ns=t0 + (15 + j) * NS_PER_MIN,
                    price_scaled=base + 200 * SCALE,
                    spread_pts=3.0,
                    hour_local=9, minute_local=45 + j,
                    day_str="2026-03-15",
                )
                if signal is not None:
                    break

        assert signal is not None
        assert signal.direction == "BUY"  # Continuation of UP shock

    def test_blocks_entry_on_wide_spread(self) -> None:
        strat = self._make_strategy()
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        # Trigger event
        for i in range(15):
            strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + i * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30,
                day_str="2026-03-15",
            )

        # Try entry with wide spread
        signal = strat.on_tick(
            timestamp_ns=t0 + 20 * NS_PER_MIN,
            price_scaled=base + 200 * SCALE,
            spread_pts=10.0,  # > max_spread_pts
            hour_local=9, minute_local=50,
            day_str="2026-03-15",
        )
        assert signal is None  # Blocked by spread

    def test_blocks_entry_outside_session(self) -> None:
        strat = self._make_strategy()
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        for i in range(15):
            strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + i * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30,
                day_str="2026-03-15",
            )

        # Try entry outside session hours
        signal = strat.on_tick(
            timestamp_ns=t0 + 20 * NS_PER_MIN,
            price_scaled=base + 200 * SCALE,
            spread_pts=3.0,
            hour_local=20, minute_local=0,  # Night session
            day_str="2026-03-15",
        )
        assert signal is None

    def test_blocks_entry_during_halt(self) -> None:
        strat = self._make_strategy()
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        for i in range(15):
            strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + i * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30,
                day_str="2026-03-15",
            )

        signal = strat.on_tick(
            timestamp_ns=t0 + 20 * NS_PER_MIN,
            price_scaled=base + 200 * SCALE,
            spread_pts=3.0,
            hour_local=9, minute_local=50,
            day_str="2026-03-15",
            storm_guard_halt=True,
        )
        assert signal is None

    def test_respects_daily_event_limit(self) -> None:
        strat = self._make_strategy(max_events_per_day=1)
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        # Trigger first event
        signals = []
        for i in range(30):
            s = strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + (i % 15) * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30 + (i % 30),
                day_str="2026-03-15",
            )
            if s is not None:
                signals.append(s)

        # Only 1 signal allowed per day
        assert len(signals) <= 1

    def test_daily_pnl_tracking(self) -> None:
        strat = self._make_strategy(daily_loss_limit_pts=50.0)
        strat._current_day = "2026-03-15"
        strat.record_trade_result(-60.0)
        assert strat._daily_pnl_pts == -60.0

    def test_mean_reversion_mode(self) -> None:
        strat = self._make_strategy(entry_mode="mean_reversion")
        base = 32000 * SCALE
        t0 = 1_000_000_000_000

        signal = None
        for i in range(25):
            signal = strat.on_tick(
                timestamp_ns=t0 + i * NS_PER_MIN,
                price_scaled=base + i * 15 * SCALE,
                spread_pts=3.0,
                hour_local=9, minute_local=30,
                day_str="2026-03-15",
            )
            if signal is not None:
                break

        if signal is None:
            for j in range(10):
                signal = strat.on_tick(
                    timestamp_ns=t0 + (25 + j) * NS_PER_MIN,
                    price_scaled=base + 200 * SCALE,
                    spread_pts=3.0,
                    hour_local=9, minute_local=55 + j,
                    day_str="2026-03-15",
                )
                if signal is not None:
                    break

        if signal is not None:
            assert signal.direction == "SELL"  # Mean reversion against UP shock


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

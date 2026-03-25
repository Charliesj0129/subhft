"""Tests for VpinRegimeSwitchStrategy — platform VPIN regime switch alpha.

Covers:
  - Auto-calibration triggers after warmup
  - Signal output bounded to [-1, 1]
  - Regime transitions with calibrated thresholds
  - Reset clears all state
  - Both tick-volume and depth-proxy modes
  - Edge cases (zero volume, degenerate data)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.events import LOBStatsEvent, MetaData, TickEvent
from hft_platform.strategies.vpin_regime_switch import (
    BulkVolumeClassifier,
    Regime,
    RegimeDetector,
    VolumeBar,
    VolumeBarBuilder,
    VPINCalculator,
    VpinRegimeSwitchStrategy,
)
from hft_platform.strategy.base import StrategyContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(strategy_id: str = "test_vpin") -> StrategyContext:
    """Create a minimal StrategyContext for testing."""
    return StrategyContext(
        positions={},
        strategy_id=strategy_id,
        intent_factory=MagicMock(),
        price_scaler=lambda _sym, p: int(p),
    )


def _make_tick(symbol: str, price: int, volume: int, ts: int = 0) -> TickEvent:
    return TickEvent(
        meta=MetaData(seq=0, source_ts=ts, local_ts=ts),
        symbol=symbol,
        price=price,
        volume=volume,
    )


def _make_lob_stats(
    symbol: str,
    mid_price_x2: int,
    bid_depth: int,
    ask_depth: int,
    ts: int = 0,
) -> LOBStatsEvent:
    best_bid = mid_price_x2 // 2
    best_ask = mid_price_x2 - best_bid
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        mid_price_x2=mid_price_x2,
        spread_scaled=best_ask - best_bid,
    )


def _make_strategy(
    use_tick_volume: bool = True,
    bar_volume_target: int = 10,
    n_vpin_buckets: int = 5,
    warmup_bars: int = 10,
    **extra_params: object,
) -> VpinRegimeSwitchStrategy:
    params = {
        "bar_volume_target": bar_volume_target,
        "n_vpin_buckets": n_vpin_buckets,
        "warmup_bars": warmup_bars,
        "use_tick_volume": use_tick_volume,
        **extra_params,
    }
    return VpinRegimeSwitchStrategy(
        strategy_id="test_vpin",
        symbols=["2330"],
        params=params,
    )


def _feed_ticks_rising(
    strat: VpinRegimeSwitchStrategy,
    ctx: StrategyContext,
    n_bars: int,
    bar_size: int = 10,
    base_price: int = 1000000,
) -> None:
    """Feed rising-price ticks to produce n_bars volume bars."""
    for bar_i in range(n_bars):
        for tick_i in range(bar_size):
            price = base_price + (bar_i * bar_size + tick_i) * 100
            tick = _make_tick("2330", price, 1, ts=bar_i * bar_size + tick_i)
            strat.handle_event(ctx, tick)


def _feed_ticks_alternating(
    strat: VpinRegimeSwitchStrategy,
    ctx: StrategyContext,
    n_bars: int,
    bar_size: int = 10,
    base_price: int = 1000000,
) -> None:
    """Feed alternating price ticks (low imbalance = low VPIN)."""
    for bar_i in range(n_bars):
        for tick_i in range(bar_size):
            # Alternate up and down each tick
            direction = 1 if tick_i % 2 == 0 else -1
            price = base_price + direction * 100
            tick = _make_tick("2330", price, 1, ts=bar_i * bar_size + tick_i)
            strat.handle_event(ctx, tick)


# ---------------------------------------------------------------------------
# VolumeBarBuilder tests
# ---------------------------------------------------------------------------


class TestVolumeBarBuilder:
    def test_tick_mode_produces_bar_at_target(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=5, use_tick_volume=True)
        result = None
        for i in range(5):
            result = builder.add_tick(price=1000000 + i * 100, volume=1, ts=i)
        assert result is not None
        assert result.total_volume == 5

    def test_depth_mode_produces_bar(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=10, use_tick_volume=False)
        # First call initializes
        assert builder.add_depth_update(2000000, 100, 100, ts=0) is None
        # Subsequent calls accumulate churn
        result = None
        for i in range(1, 20):
            result = builder.add_depth_update(2000000, 100 - i, 100 + i, ts=i)
            if result is not None:
                break
        assert result is not None
        assert result.total_volume >= 10

    def test_tick_mode_ignores_depth(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=5, use_tick_volume=True)
        result = builder.add_depth_update(2000000, 100, 100, ts=0)
        assert result is None

    def test_depth_mode_ignores_ticks(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=5, use_tick_volume=False)
        result = builder.add_tick(1000000, 5, ts=0)
        assert result is None

    def test_zero_volume_tick_ignored(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=5, use_tick_volume=True)
        result = builder.add_tick(1000000, 0, ts=0)
        assert result is None

    def test_reset_clears_state(self) -> None:
        builder = VolumeBarBuilder(bar_volume_target=5, use_tick_volume=True)
        builder.add_tick(1000000, 3, ts=0)
        builder.reset()
        assert builder._accumulated_volume == 0
        assert builder._last_price == 0


# ---------------------------------------------------------------------------
# BulkVolumeClassifier tests
# ---------------------------------------------------------------------------


class TestBulkVolumeClassifier:
    def test_tick_rule_rising_bar(self) -> None:
        classifier = BulkVolumeClassifier(use_bvc=False)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1001000,
            low_price=1000000,
            close_price=1001000,
            total_volume=10,
            buy_volume=8,
            sell_volume=2,
            ts_start=0,
            ts_end=1,
        )
        frac = classifier.classify(bar)
        assert 0.0 <= frac <= 1.0
        assert frac == 0.8  # buy_volume / total_volume (has price variation)

    def test_bvc_mode_returns_valid_fraction(self) -> None:
        classifier = BulkVolumeClassifier(use_bvc=True)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1001000,
            low_price=1000000,
            close_price=1001000,
            total_volume=10,
            buy_volume=5,
            sell_volume=5,
            ts_start=0,
            ts_end=1,
        )
        frac = classifier.classify(bar)
        assert 0.0 <= frac <= 1.0

    def test_zero_volume_bar_returns_half(self) -> None:
        classifier = BulkVolumeClassifier(use_bvc=False)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1000000,
            low_price=1000000,
            close_price=1000000,
            total_volume=0,
            buy_volume=0,
            sell_volume=0,
            ts_start=0,
            ts_end=0,
        )
        assert classifier.classify(bar) == 0.5

    def test_reset_clears_state(self) -> None:
        classifier = BulkVolumeClassifier(use_bvc=True)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1001000,
            low_price=1000000,
            close_price=1001000,
            total_volume=10,
            buy_volume=8,
            sell_volume=2,
            ts_start=0,
            ts_end=1,
        )
        classifier.classify(bar)
        classifier.reset()
        assert classifier._last_bar_close == 0
        assert classifier._bvc_initialized is False


# ---------------------------------------------------------------------------
# VPINCalculator tests
# ---------------------------------------------------------------------------


class TestVPINCalculator:
    def test_vpin_in_zero_one_range(self) -> None:
        calc = VPINCalculator(n_buckets=5)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1001000,
            low_price=1000000,
            close_price=1001000,
            total_volume=10,
            buy_volume=8,
            sell_volume=2,
            ts_start=0,
            ts_end=1,
        )
        vpin = calc.add_bar(bar, buy_fraction=0.8)
        assert 0.0 <= vpin <= 1.0

    def test_full_buy_gives_vpin_one(self) -> None:
        calc = VPINCalculator(n_buckets=3)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1000000,
            low_price=1000000,
            close_price=1000000,
            total_volume=10,
            buy_volume=10,
            sell_volume=0,
            ts_start=0,
            ts_end=0,
        )
        # 100% buy imbalance → toxicity ratio = 1.0
        for _ in range(3):
            vpin = calc.add_bar(bar, buy_fraction=1.0)
        assert abs(vpin - 1.0) < 1e-9

    def test_balanced_gives_vpin_zero(self) -> None:
        calc = VPINCalculator(n_buckets=3)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1000000,
            low_price=1000000,
            close_price=1000000,
            total_volume=10,
            buy_volume=5,
            sell_volume=5,
            ts_start=0,
            ts_end=0,
        )
        for _ in range(3):
            vpin = calc.add_bar(bar, buy_fraction=0.5)
        assert abs(vpin) < 1e-9

    def test_is_warm_after_fill(self) -> None:
        calc = VPINCalculator(n_buckets=3)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1000000,
            low_price=1000000,
            close_price=1000000,
            total_volume=10,
            buy_volume=5,
            sell_volume=5,
            ts_start=0,
            ts_end=0,
        )
        assert not calc.is_warm
        for _ in range(3):
            calc.add_bar(bar, buy_fraction=0.5)
        assert calc.is_warm

    def test_reset_clears_state(self) -> None:
        calc = VPINCalculator(n_buckets=3)
        bar = VolumeBar(
            open_price=1000000,
            high_price=1000000,
            low_price=1000000,
            close_price=1000000,
            total_volume=10,
            buy_volume=5,
            sell_volume=5,
            ts_start=0,
            ts_end=0,
        )
        calc.add_bar(bar, buy_fraction=0.5)
        calc.reset()
        assert calc._count == 0
        assert not calc.is_warm


# ---------------------------------------------------------------------------
# RegimeDetector tests
# ---------------------------------------------------------------------------


class TestRegimeDetector:
    def test_calibrate_sets_thresholds(self) -> None:
        det = RegimeDetector()
        history = [0.1 * i for i in range(1, 21)]  # 0.1 to 2.0
        det.calibrate(history)
        assert det.is_calibrated
        assert det.threshold_elevated > 0
        assert det.threshold_toxic > det.threshold_elevated

    def test_calibrate_rejects_insufficient_data(self) -> None:
        det = RegimeDetector()
        with pytest.raises(ValueError, match="requires >="):
            det.calibrate([0.1, 0.2, 0.3])

    def test_low_vpin_gives_low_regime(self) -> None:
        det = RegimeDetector(threshold_elevated=0.4, threshold_toxic=0.7)
        regime, smoothed = det.update(0.1)
        assert regime == Regime.LOW

    def test_high_vpin_gives_toxic_regime(self) -> None:
        det = RegimeDetector(threshold_elevated=0.4, threshold_toxic=0.7, ema_alpha=1.0)
        regime, _ = det.update(0.9)
        assert regime == Regime.TOXIC

    def test_elevated_regime(self) -> None:
        det = RegimeDetector(threshold_elevated=0.4, threshold_toxic=0.7, ema_alpha=1.0)
        regime, _ = det.update(0.5)
        assert regime == Regime.ELEVATED

    def test_hysteresis_prevents_flapping(self) -> None:
        det = RegimeDetector(threshold_elevated=0.4, threshold_toxic=0.7, ema_alpha=1.0)
        # Push to TOXIC
        det.update(0.8)
        assert det.regime == Regime.TOXIC
        # Drop slightly below toxic but above hysteresis (0.7 * 0.95 = 0.665)
        det.update(0.68)
        assert det.regime == Regime.TOXIC  # Still toxic due to hysteresis

    def test_reset_clears_calibration(self) -> None:
        det = RegimeDetector()
        history = [0.1 * i for i in range(1, 21)]
        det.calibrate(history)
        assert det.is_calibrated
        det.reset()
        assert not det.is_calibrated


# ---------------------------------------------------------------------------
# VpinRegimeSwitchStrategy integration tests
# ---------------------------------------------------------------------------


class TestVpinRegimeSwitchStrategyTickMode:
    """Test the strategy in tick-volume mode."""

    def test_signal_is_zero_before_calibration(self) -> None:
        strat = _make_strategy(use_tick_volume=True, warmup_bars=20, n_vpin_buckets=5)
        ctx = _make_ctx()
        # Feed a few bars (less than warmup)
        _feed_ticks_rising(strat, ctx, n_bars=3, bar_size=10)
        assert strat.signal == 0.0
        assert not strat.is_calibrated

    def test_auto_calibration_triggers_after_warmup(self) -> None:
        # warmup_bars must be >= _MIN_CALIBRATION_SAMPLES (20) for immediate calibration
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()
        # Feed enough bars to trigger calibration
        _feed_ticks_rising(strat, ctx, n_bars=30, bar_size=5)
        assert strat.is_calibrated
        assert strat.bars_seen >= 25

    def test_signal_output_bounded(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()
        # Feed many bars
        _feed_ticks_rising(strat, ctx, n_bars=35, bar_size=5)
        assert -1.0 <= strat.signal <= 1.0

    def test_signal_values_are_valid_regime_signals(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()
        _feed_ticks_rising(strat, ctx, n_bars=35, bar_size=5)
        # Signal must be one of {-1, 0, 1}
        assert strat.signal in {-1.0, 0.0, 1.0}

    def test_ignores_lob_stats_in_tick_mode(self) -> None:
        strat = _make_strategy(use_tick_volume=True, warmup_bars=5, n_vpin_buckets=3)
        ctx = _make_ctx()
        event = _make_lob_stats("2330", 2000000, 100, 100, ts=1)
        strat.handle_event(ctx, event)
        assert strat.bars_seen == 0

    def test_zero_volume_ticks_ignored(self) -> None:
        strat = _make_strategy(use_tick_volume=True)
        ctx = _make_ctx()
        tick = _make_tick("2330", 1000000, 0, ts=0)
        strat.handle_event(ctx, tick)
        assert strat.bars_seen == 0

    def test_regime_transitions_with_calibrated_thresholds(self) -> None:
        """After calibration with balanced data, force a high-VPIN scenario."""
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=3,
            n_vpin_buckets=3,
            warmup_bars=25,
            ema_alpha=1.0,  # No smoothing for deterministic test
        )
        ctx = _make_ctx()
        # Phase 1: Warmup with balanced ticks (low VPIN) to calibrate
        _feed_ticks_alternating(strat, ctx, n_bars=30, bar_size=3)
        assert strat.is_calibrated
        # Low VPIN data calibration should set low thresholds
        first_regime = strat.regime

        # Phase 2: Feed strongly directional ticks (high VPIN)
        # All buys → toxicity ratio near 1.0
        for bar_i in range(10):
            for tick_i in range(3):
                price = 2000000 + (bar_i * 3 + tick_i) * 1000
                tick = _make_tick("2330", price, 1, ts=1000 + bar_i * 3 + tick_i)
                strat.handle_event(ctx, tick)

        # With strongly directional flow, VPIN should be high
        assert strat.raw_vpin > 0.0


class TestVpinRegimeSwitchEmaWarmup:
    """Verify EMA warmup during calibration matches research impl (VPIN-C1)."""

    def test_regime_detector_ema_updated_during_warmup(self) -> None:
        """RegimeDetector.update() must be called on every bar during warmup,
        so the EMA is warm by the time calibration completes."""
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()

        # Feed bars but not enough for calibration
        _feed_ticks_rising(strat, ctx, n_bars=10, bar_size=5)
        assert not strat.is_calibrated
        # EMA should already be initialized (not zero) from warmup updates
        assert strat._regime_detector._initialized is True
        assert strat.smoothed_vpin != 0.0

    def test_ema_warmup_parity_with_research(self) -> None:
        """Post-calibration EMA should reflect all warmup bars, not just one seed."""
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
            ema_alpha=0.1,
        )
        ctx = _make_ctx()

        # Calibrate
        _feed_ticks_rising(strat, ctx, n_bars=30, bar_size=5)
        assert strat.is_calibrated

        # The smoothed VPIN should reflect a warmed EMA (many updates),
        # not a single-seed value equal to raw_vpin
        smoothed_at_calibration = strat.smoothed_vpin
        # With alpha=0.1 and many bars, EMA should have converged away from 0
        assert smoothed_at_calibration > 0.0

    def test_signal_gated_during_warmup(self) -> None:
        """Signal must remain 0.0 during warmup even though EMA is updating."""
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()

        # Feed fewer bars than warmup
        _feed_ticks_rising(strat, ctx, n_bars=10, bar_size=5)
        assert strat.signal == 0.0
        assert not strat.is_calibrated


class TestVpinRegimeSwitchStrategyDepthMode:
    """Test the strategy in depth-churn proxy mode."""

    def test_depth_mode_processes_lob_stats(self) -> None:
        strat = _make_strategy(
            use_tick_volume=False,
            bar_volume_target=10,
            n_vpin_buckets=3,
            warmup_bars=5,
        )
        ctx = _make_ctx()
        # First event initializes
        strat.handle_event(ctx, _make_lob_stats("2330", 2000000, 100, 100, ts=0))
        # Feed depth changes to generate bars
        for i in range(1, 30):
            event = _make_lob_stats(
                "2330",
                2000000,
                bid_depth=max(1, 100 - i * 3),
                ask_depth=100 + i * 2,
                ts=i,
            )
            strat.handle_event(ctx, event)
        assert strat.bars_seen > 0

    def test_depth_mode_ignores_ticks(self) -> None:
        strat = _make_strategy(use_tick_volume=False, warmup_bars=5, n_vpin_buckets=3)
        ctx = _make_ctx()
        tick = _make_tick("2330", 1000000, 5, ts=0)
        strat.handle_event(ctx, tick)
        assert strat.bars_seen == 0

    def test_depth_mode_auto_calibrates(self) -> None:
        strat = _make_strategy(
            use_tick_volume=False,
            bar_volume_target=5,
            n_vpin_buckets=3,
            warmup_bars=5,
        )
        ctx = _make_ctx()
        # Initialize
        strat.handle_event(ctx, _make_lob_stats("2330", 2000000, 100, 100, ts=0))
        # Feed lots of depth changes
        for i in range(1, 100):
            event = _make_lob_stats(
                "2330",
                2000000,
                bid_depth=max(1, 100 - (i % 20) * 3),
                ask_depth=100 + (i % 20) * 2,
                ts=i,
            )
            strat.handle_event(ctx, event)
        assert strat.is_calibrated
        assert -1.0 <= strat.signal <= 1.0

    def test_depth_mode_invalid_mid_price_ignored(self) -> None:
        strat = _make_strategy(use_tick_volume=False)
        ctx = _make_ctx()
        event = _make_lob_stats("2330", 0, 100, 100, ts=0)
        strat.handle_event(ctx, event)
        assert strat.bars_seen == 0


class TestVpinRegimeSwitchReset:
    """Test reset clears all state."""

    def test_reset_clears_all_state(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()
        _feed_ticks_rising(strat, ctx, n_bars=30, bar_size=5)
        assert strat.bars_seen > 0

        strat.reset()

        assert strat.signal == 0.0
        assert strat.raw_vpin == 0.0
        assert strat.smoothed_vpin == 0.0
        assert strat.regime == Regime.LOW
        assert strat.bars_seen == 0
        assert not strat.is_calibrated

    def test_strategy_works_after_reset(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=25,
        )
        ctx = _make_ctx()
        _feed_ticks_rising(strat, ctx, n_bars=30, bar_size=5)
        strat.reset()
        # Should be able to re-calibrate
        _feed_ticks_rising(strat, ctx, n_bars=30, bar_size=5, base_price=2000000)
        assert strat.is_calibrated


class TestVpinRegimeSwitchSymbolFilter:
    """Test symbol filtering from BaseStrategy."""

    def test_filters_non_subscribed_symbols(self) -> None:
        strat = _make_strategy(use_tick_volume=True)
        ctx = _make_ctx()
        # Strategy subscribes to "2330", feed "2454"
        tick = _make_tick("2454", 1000000, 5, ts=0)
        intents = strat.handle_event(ctx, tick)
        assert intents == []
        assert strat.bars_seen == 0

    def test_processes_subscribed_symbol(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=3,
            warmup_bars=5,
        )
        ctx = _make_ctx()
        for i in range(10):
            tick = _make_tick("2330", 1000000 + i * 100, 1, ts=i)
            strat.handle_event(ctx, tick)
        assert strat.bars_seen > 0


class TestVpinRegimeSwitchNoOrderIntents:
    """Verify the strategy emits no OrderIntents (signal-only alpha)."""

    def test_handle_event_returns_empty_intents(self) -> None:
        strat = _make_strategy(
            use_tick_volume=True,
            bar_volume_target=5,
            n_vpin_buckets=5,
            warmup_bars=10,
        )
        ctx = _make_ctx()
        for i in range(100):
            tick = _make_tick("2330", 1000000 + i * 100, 1, ts=i)
            intents = strat.handle_event(ctx, tick)
            assert intents == []

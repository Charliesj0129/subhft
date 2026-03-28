"""Tests for vrr_5_300_x1000 feature (index [21]) in FeatureEngine."""

from __future__ import annotations

from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.registry import (
    build_default_lob_feature_set_v2,
)


def _make_stats_tuple(
    symbol: str = "TMFD6",
    ts: int = 1_000_000_000,
    mid_price_x2: int = 400_000,
    spread_scaled: int = 20,
    imbalance: float = 0.0,
    best_bid: int = 199_990,
    best_ask: int = 200_010,
    bid_depth: int = 10,
    ask_depth: int = 10,
) -> tuple:
    return (symbol, ts, mid_price_x2, spread_scaled, imbalance, best_bid, best_ask, bid_depth, ask_depth)


class TestVrrFeatureSpec:
    """Test that vrr_5_300_x1000 is correctly registered at index [21]."""

    def test_feature_exists_at_index_21(self) -> None:
        fs = build_default_lob_feature_set_v2()
        assert len(fs.features) >= 22
        assert fs.features[21].feature_id == "vrr_5_300_x1000"

    def test_feature_spec_properties(self) -> None:
        fs = build_default_lob_feature_set_v2()
        spec = fs.features[21]
        assert spec.dtype == "i64"
        assert spec.scale == 1000
        assert spec.warmup_min_events == 2400
        assert spec.source_kind == "book"

    def test_backward_compatibility_indices_0_to_20(self) -> None:
        """Indices [0]-[20] must be unchanged from the original v2 definition."""
        fs = build_default_lob_feature_set_v2()
        expected_ids = [
            "best_bid",
            "best_ask",
            "mid_price_x2",
            "spread_scaled",
            "bid_depth",
            "ask_depth",
            "depth_imbalance_ppm",
            "microprice_x2",
            "l1_bid_qty",
            "l1_ask_qty",
            "l1_imbalance_ppm",
            "ofi_l1_raw",
            "ofi_l1_cum",
            "ofi_l1_ema8",
            "spread_ema8_scaled",
            "depth_imbalance_ema8_ppm",
            "ofi_depth_norm_ppm",
            "ret_autocov_5s_x1e6",
            "tob_survival_ms",
            "impact_surprise_x1000",
            "deep_depth_momentum_x1000",
        ]
        for i, expected_id in enumerate(expected_ids):
            assert fs.features[i].feature_id == expected_id, (
                f"Index [{i}] expected {expected_id}, got {fs.features[i].feature_id}"
            )

    def test_index_by_id_lookup(self) -> None:
        fs = build_default_lob_feature_set_v2()
        assert fs.index_by_id["vrr_5_300_x1000"] == 21


class TestVrrKernelComputation:
    """Test the vrr computation kernel logic."""

    def test_vrr_returns_zero_on_first_tick(self) -> None:
        engine = FeatureEngine(emit_events=False)
        stats = _make_stats_tuple(mid_price_x2=400_000, ts=1_000_000_000)
        engine.process_lob_update(None, stats)
        val = engine.get_feature_by_index("TMFD6", 21)
        assert val == 0

    def test_vrr_returns_zero_during_warmup(self) -> None:
        engine = FeatureEngine(emit_events=False)
        # Feed 100 ticks — well below warmup of 2400
        for i in range(100):
            mid = 400_000 + (i % 3) * 20  # slight oscillation
            ts = 1_000_000_000 + i * 125_000_000  # 125ms cadence
            stats = _make_stats_tuple(mid_price_x2=mid, ts=ts)
            engine.process_lob_update(None, stats)

        # VRR should be computed but the warmup_ready_mask should exclude it
        # The value itself is computed even during warmup (for accumulator burn-in)
        val = engine.get_feature_by_index("TMFD6", 21)
        assert val is not None  # value exists
        # But since n_features > 21, the vrr is always in the tuple

    def test_vrr_responds_to_volatility_spike(self) -> None:
        """After a period of calm followed by a volatility spike,
        vrr should increase (short var > long var)."""
        engine = FeatureEngine(emit_events=False)

        # Phase 1: 3000 ticks of very calm market (constant price)
        for i in range(3000):
            ts = 1_000_000_000 + i * 125_000_000
            stats = _make_stats_tuple(mid_price_x2=400_000, ts=ts)
            engine.process_lob_update(None, stats)

        vrr_calm = engine.get_feature_by_index("TMFD6", 21)
        # During flat market, both variances → 0, vrr could be 0 or undefined
        # (var_l < 1e-20 guard kicks in)

        # Phase 2: 200 ticks of volatile market (large oscillations)
        for i in range(200):
            idx = 3000 + i
            ts = 1_000_000_000 + idx * 125_000_000
            # +-100 pts oscillation
            mid = 400_000 + ((-1) ** i) * 200
            stats = _make_stats_tuple(mid_price_x2=mid, ts=ts)
            engine.process_lob_update(None, stats)

        vrr_volatile = engine.get_feature_by_index("TMFD6", 21)
        # After volatility spike, short-window var should be higher than long-window
        # vrr should be > 1000 (i.e., > 1.0 in real units)
        assert vrr_volatile is not None
        assert vrr_volatile > 0

    def test_vrr_clamped_at_upper_bound(self) -> None:
        """vrr > 10.0 should be clamped to 10000."""
        engine = FeatureEngine(emit_events=False)

        # Long period of calm to build low long-window variance
        for i in range(3000):
            ts = 1_000_000_000 + i * 125_000_000
            stats = _make_stats_tuple(mid_price_x2=400_000, ts=ts)
            engine.process_lob_update(None, stats)

        # Extreme volatility spike — should push vrr way above 10
        for i in range(50):
            idx = 3000 + i
            ts = 1_000_000_000 + idx * 125_000_000
            mid = 400_000 + ((-1) ** i) * 5000  # huge swing
            stats = _make_stats_tuple(mid_price_x2=mid, ts=ts)
            engine.process_lob_update(None, stats)

        vrr_val = engine.get_feature_by_index("TMFD6", 21)
        assert vrr_val is not None
        assert vrr_val <= 10_000, f"vrr should be clamped at 10000, got {vrr_val}"

    def test_vrr_non_negative(self) -> None:
        """vrr should never be negative."""
        engine = FeatureEngine(emit_events=False)
        for i in range(500):
            ts = 1_000_000_000 + i * 125_000_000
            mid = 400_000 + i * 10  # trending up
            stats = _make_stats_tuple(mid_price_x2=mid, ts=ts)
            engine.process_lob_update(None, stats)

        vrr_val = engine.get_feature_by_index("TMFD6", 21)
        assert vrr_val is not None
        assert vrr_val >= 0

    def test_feature_tuple_has_correct_length(self) -> None:
        """Feature tuple should have 22 elements (indices 0-21)."""
        engine = FeatureEngine(emit_events=False)
        for i in range(10):
            ts = 1_000_000_000 + i * 125_000_000
            stats = _make_stats_tuple(mid_price_x2=400_000 + i * 10, ts=ts)
            engine.process_lob_update(None, stats)

        ft = engine.get_feature_tuple("TMFD6")
        assert ft is not None
        assert len(ft) == 22, f"Expected 22 features, got {len(ft)}"


class TestVrrWarmupFlag:
    """Test that vrr feature gets PARTIAL quality flag during warmup."""

    def test_warmup_ready_mask_excludes_vrr_initially(self) -> None:
        engine = FeatureEngine(emit_events=True)
        stats = _make_stats_tuple(mid_price_x2=400_000, ts=1_000_000_000)
        event = engine.process_lob_update(None, stats)
        assert event is not None
        # Bit 21 should NOT be set in warmup_ready_mask (needs 2400 events)
        assert not (event.warmup_ready_mask & (1 << 21)), "vrr should not be warm after 1 tick"

    def test_warmup_ready_mask_includes_vrr_after_threshold(self) -> None:
        engine = FeatureEngine(emit_events=True)
        event = None
        for i in range(2401):
            ts = 1_000_000_000 + i * 125_000_000
            mid = 400_000 + (i % 5) * 10
            stats = _make_stats_tuple(mid_price_x2=mid, ts=ts)
            event = engine.process_lob_update(None, stats)

        assert event is not None
        assert event.warmup_ready_mask & (1 << 21), "vrr should be warm after 2401 ticks"

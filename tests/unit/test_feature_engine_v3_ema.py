"""Tests for FeatureEngine v3 multi-window EMA aggregation features."""

from __future__ import annotations

import numpy as np

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.engine import FeatureEngine


def _make_stats(
    symbol: str, best_bid: int, best_ask: int, bid_depth: int, ask_depth: int, ts: int = 1_000_000_000
) -> LOBStatsEvent:
    """Minimal LOBStatsEvent for testing."""
    mid_x2 = best_bid + best_ask
    spread = best_ask - best_bid
    imb = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1)
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        mid_price_x2=mid_x2,
        spread_scaled=spread,
        imbalance=imb,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


def _make_event(bids: list[list[int]], asks: list[list[int]]) -> BidAskEvent:
    """Minimal BidAskEvent for testing."""
    return BidAskEvent(
        meta=MetaData(source_ts=1_000_000_000, local_ts=1_000_000_000, seq=0),
        symbol="SYM",
        bids=np.array(bids, dtype=np.int64) if bids else np.zeros((0, 2), dtype=np.int64),
        asks=np.array(asks, dtype=np.int64) if asks else np.zeros((0, 2), dtype=np.int64),
    )


class TestV3EmaFeatures:
    def _make_engine(self) -> FeatureEngine:
        return FeatureEngine(feature_set_id="lob_shared_v3", kernel_backend="python")

    def test_v3_produces_27_features(self) -> None:
        engine = self._make_engine()
        stats = _make_stats("SYM", 100_0000, 101_0000, 50, 50)
        event = _make_event([[100_0000, 50]], [[101_0000, 50]])
        engine.process_lob_update(event, stats)
        vals = engine.get_feature_tuple("SYM")
        assert vals is not None
        assert len(vals) == 27

    def test_ema_converges_to_constant_input(self) -> None:
        engine = self._make_engine()
        for i in range(500):
            stats = _make_stats("SYM", 100_0000, 103_0000, 50, 50, ts=1_000_000_000 + i * 125_000_000)
            event = _make_event(
                [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
                [[103_0000, 50], [104_0000, 30], [105_0000, 20], [106_0000, 10], [107_0000, 5]],
            )
            engine.process_lob_update(event, stats)
        vals = engine.get_feature_tuple("SYM")
        assert vals is not None
        spread_ema30s = vals[25]
        spread_ema300s = vals[26]
        assert abs(spread_ema30s - 3_0000) < 500, f"spread_ema30s={spread_ema30s} not near 30000"
        assert spread_ema300s > 0

    def test_ema_alpha_constants(self) -> None:
        engine = self._make_engine()
        assert abs(engine._alpha_5s - 2.0 / 41.0) < 1e-10
        assert abs(engine._alpha_30s - 2.0 / 241.0) < 1e-10
        assert abs(engine._alpha_300s - 2.0 / 2401.0) < 1e-10

    def test_warmup_mask_respects_ema_windows(self) -> None:
        engine = self._make_engine()
        fe = None
        for i in range(45):
            stats = _make_stats("SYM", 100_0000, 101_0000, 50, 50, ts=1_000_000_000 + i * 125_000_000)
            event = _make_event([[100_0000, 50]], [[101_0000, 50]])
            fe = engine.process_lob_update(event, stats)
        assert fe is not None
        mask = fe.warmup_ready_mask
        assert mask & (1 << 22), "ofi_l1_ema5s should be warm at tick 45"
        assert mask & (1 << 24), "imbalance_ema5s_ppm should be warm at tick 45"
        assert not (mask & (1 << 23)), "ofi_l1_ema30s should NOT be warm at tick 45"
        assert not (mask & (1 << 25)), "spread_ema30s should NOT be warm at tick 45"

    def test_v2_features_unchanged(self) -> None:
        engine_v2 = FeatureEngine(feature_set_id="lob_shared_v2", kernel_backend="python")
        engine_v3 = self._make_engine()
        for i in range(10):
            stats = _make_stats("SYM", 100_0000, 101_0000, 50, 50, ts=1_000_000_000 + i * 125_000_000)
            event = _make_event(
                [[100_0000, 50], [99_0000, 30], [98_0000, 20], [97_0000, 10], [96_0000, 5]],
                [[101_0000, 50], [102_0000, 30], [103_0000, 20], [106_0000, 10], [107_0000, 5]],
            )
            engine_v2.process_lob_update(event, stats)
            engine_v3.process_lob_update(event, stats)
        v2_vals = engine_v2.get_feature_tuple("SYM")
        v3_vals = engine_v3.get_feature_tuple("SYM")
        assert v2_vals is not None and v3_vals is not None
        for i in range(22):
            assert v2_vals[i] == v3_vals[i], f"Feature [{i}] diverged: v2={v2_vals[i]} v3={v3_vals[i]}"

    def test_ofi_ema_responds_to_flow_direction(self) -> None:
        engine = self._make_engine()
        for i in range(100):
            bid_qty = 50 + i
            stats = _make_stats("SYM", 100_0000, 101_0000, bid_qty, 50, ts=1_000_000_000 + i * 125_000_000)
            event = _make_event([[100_0000, bid_qty]], [[101_0000, 50]])
            engine.process_lob_update(event, stats)
        vals = engine.get_feature_tuple("SYM")
        assert vals is not None
        ofi_ema5s = vals[22]
        assert ofi_ema5s > 0, f"ofi_l1_ema5s={ofi_ema5s} should be positive with growing bid depth"

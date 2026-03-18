"""Unit tests for trade_intensity_surprise alpha."""
from __future__ import annotations

import math

import pytest

from research.alphas.trade_intensity_surprise.impl import (
    ALPHA_CLASS,
    TradeIntensitySurpriseAlpha,
)
from research.registry.schemas import AlphaProtocol


class TestProtocolConformance:
    """Gate B: protocol and manifest checks."""

    def test_implements_alpha_protocol(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        assert isinstance(alpha, AlphaProtocol)

    def test_manifest_fields(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        m = alpha.manifest
        assert m.alpha_id == "trade_intensity_surprise"
        assert m.complexity == "O(1)"
        assert "bid_qty" in m.data_fields
        assert "ask_qty" in m.data_fields
        assert len(m.paper_refs) >= 1
        assert m.latency_profile is not None
        assert m.feature_set_version == "lob_shared_v1"

    def test_alpha_class_export(self) -> None:
        assert ALPHA_CLASS is TradeIntensitySurpriseAlpha

    def test_has_slots(self) -> None:
        assert hasattr(TradeIntensitySurpriseAlpha, "__slots__")


class TestSignalBehavior:
    """Core signal correctness tests."""

    def test_initial_signal_zero(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        assert alpha.get_signal() == 0.0

    def test_warmup_period_zero(self) -> None:
        """Signal should be 0 during warmup (< 64 ticks)."""
        alpha = TradeIntensitySurpriseAlpha()
        for _ in range(63):
            sig = alpha.update(100.0, 100.0)
        assert sig == 0.0

    def test_signal_after_warmup_with_activity_surge(self) -> None:
        """Bid-dominant + sudden depth activity surge → positive signal."""
        alpha = TradeIntensitySurpriseAlpha()
        # Warmup with low-activity oscillation to establish baseline
        for i in range(100):
            alpha.update(100.0 + (i % 2) * 5.0, 100.0 + ((i + 1) % 2) * 5.0)
        # Sudden burst: bid side surges (surprise!) — only a few ticks
        # so fast EMA jumps but slow EMA hasn't caught up yet
        for _ in range(10):
            sig = alpha.update(300.0, 50.0)
        assert sig > 0.0, "Bid-dominant + sudden activity surge should produce positive signal"

    def test_ask_dominant_negative(self) -> None:
        """Ask-dominant + sudden depth activity surge → negative signal."""
        alpha = TradeIntensitySurpriseAlpha()
        for i in range(100):
            alpha.update(100.0 + (i % 2) * 5.0, 100.0 + ((i + 1) % 2) * 5.0)
        # Sudden burst on ask side
        for _ in range(10):
            sig = alpha.update(50.0, 300.0)
        assert sig < 0.0, "Ask-dominant + sudden activity surge should produce negative signal"

    def test_no_depth_change_near_zero(self) -> None:
        """Constant depth → no activity → signal near zero."""
        alpha = TradeIntensitySurpriseAlpha()
        for _ in range(200):
            sig = alpha.update(100.0, 100.0)
        # With zero activity, act_fast ≈ act_slow ≈ 0 → ir ≈ 0
        assert abs(sig) < 0.01, "No depth change should yield near-zero signal"

    def test_activity_deceleration(self) -> None:
        """High activity → sudden calm with bid-dominant → signal goes negative."""
        alpha = TradeIntensitySurpriseAlpha()
        # Phase 1: high activity period (warmup)
        for i in range(100):
            bid = 100.0 + (i % 5) * 40.0
            ask = 100.0 + ((i + 2) % 5) * 40.0
            alpha.update(bid, ask)
        # Phase 2: sudden calm but bid-dominant
        for _ in range(200):
            sig = alpha.update(200.0, 50.0)
        # Activity decelerates → ir < 0, qi > 0 → signal < 0
        assert sig < 0.0, "Deceleration + bid-dominant should invert signal"


class TestInputModes:
    """Test various input conventions."""

    def test_positional_args(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update(100.0, 50.0)
        assert isinstance(sig, float)

    def test_keyword_args(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update(bid_qty=100.0, ask_qty=50.0)
        assert isinstance(sig, float)

    def test_bids_asks_arrays(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        import numpy as np
        bids = np.array([[100_0000, 50.0]])
        asks = np.array([[100_1000, 30.0]])
        sig = alpha.update(bids=bids, asks=asks)
        assert isinstance(sig, float)

    def test_no_args_returns_zero(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update()
        assert sig == 0.0

    def test_extra_kwargs_ignored(self) -> None:
        """Bridge passes volume=0.0 and other fields — should not crash."""
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update(bid_qty=100.0, ask_qty=50.0, volume=0.0, mid_price=100.0)
        assert isinstance(sig, float)


class TestReset:
    """Test reset behavior."""

    def test_reset_clears_state(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        # Build up state with varying depth
        for i in range(100):
            alpha.update(100.0 + i * 2, 100.0)
        for i in range(100):
            alpha.update(300.0 + (i % 5) * 20, 50.0)
        assert alpha.get_signal() != 0.0
        alpha.reset()
        assert alpha.get_signal() == 0.0

    def test_reset_restarts_warmup(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        for i in range(200):
            alpha.update(100.0 + i, 50.0)
        alpha.reset()
        sig = alpha.update(200.0, 50.0)
        assert sig == 0.0, "After reset, warmup should restart"


class TestEdgeCases:
    """Edge case handling."""

    def test_zero_depth(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update(0.0, 0.0)
        assert math.isfinite(sig)

    def test_extreme_imbalance(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        sig = alpha.update(1e6, 1.0)
        assert math.isfinite(sig)

    def test_very_large_depth_change(self) -> None:
        alpha = TradeIntensitySurpriseAlpha()
        alpha.update(100.0, 100.0)
        for _ in range(100):
            sig = alpha.update(1e6, 100.0)
        assert math.isfinite(sig)

    def test_alternating_sides(self) -> None:
        """Rapidly alternating imbalance should keep signal bounded."""
        alpha = TradeIntensitySurpriseAlpha()
        for i in range(200):
            if i % 2 == 0:
                sig = alpha.update(200.0, 50.0)
            else:
                sig = alpha.update(50.0, 200.0)
        assert math.isfinite(sig)
        assert abs(sig) < 10.0  # bounded

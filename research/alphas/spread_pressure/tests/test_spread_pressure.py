"""Gate B tests for spread_pressure alpha.

Tests are written TDD-first (RED before impl.py exists).

Hypothesis: Spread widening vs EMA-8 baseline combined with depth imbalance
direction predicts short-term adverse selection pressure.

Formula:
    spread_diff = spread_ema8_scaled - spread_scaled   (positive = tighter than EMA)
    signal = spread_diff * sign(depth_imbalance_ema8_ppm) / max(|spread_ema8_scaled|, 1)
"""
from __future__ import annotations

import pytest

from research.alphas.spread_pressure.impl import SpreadPressureAlpha
from research.registry.schemas import AlphaTier, AlphaStatus


# ---------------------------------------------------------------------------
# Manifest Gate B tests
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    alpha = SpreadPressureAlpha()
    assert alpha.manifest.alpha_id == "spread_pressure"


def test_manifest_tier2() -> None:
    alpha = SpreadPressureAlpha()
    assert alpha.manifest.tier == AlphaTier.TIER_2


def test_manifest_complexity_o1() -> None:
    alpha = SpreadPressureAlpha()
    assert alpha.manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    """latency_profile must not be None; required before Gate D."""
    alpha = SpreadPressureAlpha()
    assert alpha.manifest.latency_profile is not None
    assert len(alpha.manifest.latency_profile) > 0


def test_manifest_data_fields_complete() -> None:
    alpha = SpreadPressureAlpha()
    fields = alpha.manifest.data_fields
    assert "spread_scaled" in fields
    assert "spread_ema8_scaled" in fields
    assert "depth_imbalance_ema8_ppm" in fields


def test_manifest_status_is_governed() -> None:
    """Status must be a valid governance state (DRAFT through GATE_B+)."""
    alpha = SpreadPressureAlpha()
    assert alpha.manifest.status in {
        AlphaStatus.DRAFT,
        AlphaStatus.GATE_A,
        AlphaStatus.GATE_B,
        AlphaStatus.GATE_C,
        AlphaStatus.GATE_D,
        AlphaStatus.GATE_E,
        AlphaStatus.PRODUCTION,
    }


# ---------------------------------------------------------------------------
# Signal logic Gate B tests
# ---------------------------------------------------------------------------


def test_update_returns_float() -> None:
    alpha = SpreadPressureAlpha()
    result = alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    assert isinstance(result, float)


def test_spread_tighter_positive_imbalance_positive_signal() -> None:
    """spread < ema (spread_diff > 0) AND imbalance > 0 → signal > 0."""
    alpha = SpreadPressureAlpha()
    # spread_ema8=1200, spread=1000 → diff=200 (positive = tighter)
    # imbalance=+5000 (bid pressure) → sign=+1
    # signal = 200 * 1 / 1200 > 0
    signal = alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    assert signal > 0.0


def test_spread_tighter_negative_imbalance_negative_signal() -> None:
    """spread < ema AND imbalance < 0 → signal < 0."""
    alpha = SpreadPressureAlpha()
    signal = alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=-5000)
    assert signal < 0.0


def test_spread_wider_positive_imbalance_negative_signal() -> None:
    """spread > ema (spread_diff < 0) AND imbalance > 0 → signal < 0."""
    alpha = SpreadPressureAlpha()
    # spread_ema8=1000, spread=1400 → diff=-400 (negative = wider)
    # imbalance=+5000 → sign=+1
    # signal = -400 * 1 / 1000 < 0
    signal = alpha.update(spread_scaled=1400, spread_ema8_scaled=1000, depth_imbalance_ema8_ppm=5000)
    assert signal < 0.0


def test_spread_wider_negative_imbalance_positive_signal() -> None:
    """spread > ema AND imbalance < 0 → signal > 0."""
    alpha = SpreadPressureAlpha()
    signal = alpha.update(spread_scaled=1400, spread_ema8_scaled=1000, depth_imbalance_ema8_ppm=-5000)
    assert signal > 0.0


def test_zero_spread_diff_zero_signal() -> None:
    """spread == ema → spread_diff = 0 → signal = 0.0."""
    alpha = SpreadPressureAlpha()
    signal = alpha.update(spread_scaled=1200, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    assert signal == 0.0


def test_zero_imbalance_zero_signal() -> None:
    """depth_imbalance_ema8_ppm == 0 → sign = 0 → signal = 0.0."""
    alpha = SpreadPressureAlpha()
    signal = alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=0)
    assert signal == 0.0


def test_signal_normalized_not_raw_diff() -> None:
    """Signal must be normalized by spread_ema8 so a 2× larger spread scale
    does NOT produce a 2× larger signal for the same relative deviation."""
    alpha1 = SpreadPressureAlpha()
    alpha2 = SpreadPressureAlpha()
    # Case 1: ema=1200, spread=1000 → diff=200/1200
    s1 = alpha1.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    # Case 2: ema=2400, spread=2000 → diff=400/2400 — same ratio, same signal
    s2 = alpha2.update(spread_scaled=2000, spread_ema8_scaled=2400, depth_imbalance_ema8_ppm=5000)
    assert abs(s1 - s2) < 1e-9, f"Normalization failed: s1={s1}, s2={s2}"


def test_reset_clears_signal() -> None:
    alpha = SpreadPressureAlpha()
    alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    alpha.reset()
    assert alpha.get_signal() == 0.0


def test_get_signal_same_as_update_return() -> None:
    alpha = SpreadPressureAlpha()
    returned = alpha.update(spread_scaled=1000, spread_ema8_scaled=1200, depth_imbalance_ema8_ppm=5000)
    cached = alpha.get_signal()
    assert returned == cached


def test_deterministic_repeated_calls() -> None:
    """Same inputs always produce the same output."""
    alpha = SpreadPressureAlpha()
    results = [
        alpha.update(spread_scaled=900, spread_ema8_scaled=1100, depth_imbalance_ema8_ppm=3000)
        for _ in range(5)
    ]
    assert len(set(results)) == 1, f"Non-deterministic: {results}"


def test_zero_ema_denominator_guard() -> None:
    """When spread_ema8_scaled == 0, denominator should be clamped to 1 (no div-by-zero)."""
    alpha = SpreadPressureAlpha()
    # Should not raise ZeroDivisionError
    signal = alpha.update(spread_scaled=100, spread_ema8_scaled=0, depth_imbalance_ema8_ppm=5000)
    assert isinstance(signal, float)


def test_negative_spread_ema_abs_guard() -> None:
    """Denominator uses abs(spread_ema8_scaled); negative ema handled correctly."""
    alpha = SpreadPressureAlpha()
    # spread_ema8=-1200, spread=-1000 → diff = -1200 - (-1000) = -200
    # sign(imb=5000) = +1
    # denom = max(|-1200|, 1) = 1200
    # signal = -200 * 1 / 1200 < 0
    signal = alpha.update(spread_scaled=-1000, spread_ema8_scaled=-1200, depth_imbalance_ema8_ppm=5000)
    assert signal < 0.0


def test_large_integer_inputs_no_overflow() -> None:
    """Scaled integers at realistic x10000 range should not overflow float."""
    alpha = SpreadPressureAlpha()
    # Spread of 10 ticks at x10000 = 100000; EMA = 120000
    signal = alpha.update(
        spread_scaled=100000,
        spread_ema8_scaled=120000,
        depth_imbalance_ema8_ppm=500000,
    )
    assert isinstance(signal, float)
    assert -10.0 < signal < 10.0, f"Signal out of reasonable range: {signal}"


def test_alpha_protocol_get_signal_initial() -> None:
    """Before any update, get_signal() should return 0.0 (safe default)."""
    alpha = SpreadPressureAlpha()
    assert alpha.get_signal() == 0.0


def test_alpha_class_export() -> None:
    """ALPHA_CLASS must be exported and point to SpreadPressureAlpha."""
    from research.alphas.spread_pressure.impl import ALPHA_CLASS
    assert ALPHA_CLASS is SpreadPressureAlpha

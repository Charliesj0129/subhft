"""Gate B correctness tests for ToxicityTimescaleDivergenceAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_timescale_divergence.impl import (
    ALPHA_CLASS,
    ToxicityTimescaleDivergenceAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = ToxicityTimescaleDivergenceAlpha().manifest
    assert m.alpha_id == "toxicity_timescale_divergence"
    assert m.data_fields == ("bid_qty", "ask_qty", "spread_scaled")
    assert m.paper_refs == ("129", "132")
    from research.registry.schemas import AlphaStatus

    assert m.status == AlphaStatus.DRAFT


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    assert alpha.get_signal() == 0.0


def test_single_update_returns_float() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    sig = alpha.update(500.0, 100.0, 50.0)
    assert isinstance(sig, float)


def test_fast_above_slow_positive_signal() -> None:
    """Feed increasing qi -> qi_fast > qi_slow -> positive divergence."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Start with balanced, then shift to strong bid imbalance
    for _ in range(20):
        alpha.update(500.0, 500.0, 50.0)
    # Now feed strong positive qi — fast EMA reacts first
    for _ in range(5):
        sig = alpha.update(900.0, 100.0, 50.0)
    assert sig > 0.0


def test_fast_below_slow_negative_signal() -> None:
    """Feed decreasing qi -> qi_fast < qi_slow -> negative divergence."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Start with balanced, then shift to strong ask imbalance
    for _ in range(20):
        alpha.update(500.0, 500.0, 50.0)
    # Now feed strong negative qi — fast EMA drops first
    for _ in range(5):
        sig = alpha.update(100.0, 900.0, 50.0)
    assert sig < 0.0


def test_convergence_zero_divergence() -> None:
    """After 200+ constant ticks, divergence approaches 0."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    for _ in range(300):
        alpha.update(600.0, 400.0, 50.0)
    sig = alpha.get_signal()
    # Both EMAs converge to the same qi, so divergence ~ 0
    assert abs(sig) < 0.01


def test_spread_gate_amplifies() -> None:
    """Wider spread -> larger gate -> larger |signal|."""
    # Baseline: normal spread
    a_normal = ToxicityTimescaleDivergenceAlpha()
    for _ in range(100):
        a_normal.update(500.0, 500.0, 50.0)
    for _ in range(5):
        sig_normal = a_normal.update(900.0, 100.0, 50.0)

    # Wide spread: 4x baseline
    a_wide = ToxicityTimescaleDivergenceAlpha()
    for _ in range(100):
        a_wide.update(500.0, 500.0, 50.0)
    for _ in range(5):
        sig_wide = a_wide.update(900.0, 100.0, 200.0)

    assert abs(sig_wide) > abs(sig_normal)


def test_spread_gate_floor() -> None:
    """Even at baseline spread, gate=0.1 -> some signal passes."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Feed constant spread so EMA_64 matches current spread
    for _ in range(300):
        alpha.update(500.0, 500.0, 50.0)
    # Now create divergence with same spread (excess = 0, gate = 0.1)
    for _ in range(5):
        sig = alpha.update(900.0, 100.0, 50.0)
    # Gate = 0.1 (floor), signal should be non-zero but small
    assert sig != 0.0
    assert abs(sig) > 0.0


def test_signal_clipped_at_bounds() -> None:
    """Signal is clipped to [-1, 1]."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Try to push signal to extremes
    for _ in range(500):
        sig = alpha.update(10000.0, 1.0, 10000.0)
    assert sig <= 1.0
    assert sig >= -1.0

    alpha2 = ToxicityTimescaleDivergenceAlpha()
    for _ in range(500):
        sig2 = alpha2.update(1.0, 10000.0, 10000.0)
    assert sig2 <= 1.0
    assert sig2 >= -1.0


def test_zero_quantities_no_crash() -> None:
    """bid_qty=0, ask_qty=0 doesn't crash."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    sig = alpha.update(0.0, 0.0, 50.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_reset_clears_state() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    alpha.update(800.0, 100.0, 100.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should equal fresh instance
    fresh = ToxicityTimescaleDivergenceAlpha()
    s1 = alpha.update(300.0, 300.0, 50.0)
    s2 = fresh.update(300.0, 300.0, 50.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_get_signal_matches_update() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    ret = alpha.update(500.0, 100.0, 80.0)
    assert ret == alpha.get_signal()


def test_keyword_args() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    # First tick initializes EMAs (divergence=0), need a shift to get signal
    alpha.update(bid_qty=500.0, ask_qty=500.0, spread_scaled=50.0)
    sig = alpha.update(
        bid_qty=900.0,
        ask_qty=100.0,
        spread_scaled=50.0,
    )
    assert isinstance(sig, float)
    assert sig != 0.0


def test_positional_args() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    # First tick initializes EMAs (divergence=0), need a shift to get signal
    alpha.update(500.0, 500.0, 50.0)
    sig = alpha.update(900.0, 100.0, 50.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_wrong_positional_count_raises() -> None:
    """1-2 positional args raises ValueError."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)


def test_spread_zero_no_crash() -> None:
    alpha = ToxicityTimescaleDivergenceAlpha()
    sig = alpha.update(500.0, 100.0, 0.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_direction_reversal() -> None:
    """Signal changes sign when qi reverses."""
    alpha = ToxicityTimescaleDivergenceAlpha()
    # Establish balanced baseline
    for _ in range(50):
        alpha.update(500.0, 500.0, 50.0)
    # Push positive
    for _ in range(10):
        alpha.update(900.0, 100.0, 50.0)
    sig_pos = alpha.get_signal()
    assert sig_pos > 0.0
    # Now reverse to negative
    for _ in range(10):
        alpha.update(100.0, 900.0, 50.0)
    sig_neg = alpha.get_signal()
    assert sig_neg < 0.0


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ToxicityTimescaleDivergenceAlpha


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ToxicityTimescaleDivergenceAlpha()
    assert isinstance(alpha, AlphaProtocol)

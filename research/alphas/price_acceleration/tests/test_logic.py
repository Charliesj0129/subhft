"""Gate B correctness tests for PriceAccelerationAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.price_acceleration.impl import (
    _EMA_ALPHA_8,
    ALPHA_CLASS,
    PriceAccelerationAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert PriceAccelerationAlpha().manifest.alpha_id == "price_acceleration"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert PriceAccelerationAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = PriceAccelerationAlpha().manifest.data_fields
    assert "mid_price" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert PriceAccelerationAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert PriceAccelerationAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = PriceAccelerationAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is PriceAccelerationAlpha


# ---------------------------------------------------------------------------
# Warmup behavior (need 3 ticks for meaningful signal)
# ---------------------------------------------------------------------------


def test_first_update_zero() -> None:
    """First tick stores mid_price, returns 0."""
    alpha = PriceAccelerationAlpha()
    sig = alpha.update(200000)
    assert sig == 0.0


def test_second_update_zero() -> None:
    """Second tick computes first delta but no acceleration yet, returns 0."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    sig = alpha.update(200100)
    assert sig == 0.0


def test_third_update_nonzero_if_accel() -> None:
    """Third update with changing delta must produce non-zero signal."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    alpha.update(200100)  # delta = 100
    sig = alpha.update(200300)  # delta = 200, accel = 100
    assert sig != 0.0


def test_initial_zero() -> None:
    """get_signal() before any update is zero."""
    alpha = PriceAccelerationAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_constant_velocity_zero_accel() -> None:
    """Constant mid_price increments = zero acceleration, signal -> 0."""
    alpha = PriceAccelerationAlpha()
    for i in range(200):
        alpha.update(200000 + i * 100)
    assert abs(alpha.get_signal()) < 0.01


def test_increasing_delta_positive() -> None:
    """Increasing deltas (accelerating upward) should produce positive signal."""
    alpha = PriceAccelerationAlpha()
    # Quadratic growth: 0, 1, 4, 9, 16, ... -> deltas 1, 3, 5, 7, ... -> accel = 2
    for i in range(30):
        alpha.update(200000 + i * i * 10)
    assert alpha.get_signal() > 0.0


def test_decreasing_delta_negative() -> None:
    """Decreasing deltas (decelerating / reversing) should produce negative signal."""
    alpha = PriceAccelerationAlpha()
    # Quadratic decay: -i^2 -> deltas getting more negative -> negative accel
    for i in range(30):
        alpha.update(200000 - i * i * 10)
    assert alpha.get_signal() < 0.0


def test_symmetric() -> None:
    """Symmetric acceleration up vs down should produce symmetric signals."""
    a_up = PriceAccelerationAlpha()
    a_down = PriceAccelerationAlpha()
    # Up: 0, 100, 300 (deltas: 100, 200 -> accel = 100)
    a_up.update(200000)
    a_up.update(200100)
    sig_up = a_up.update(200300)
    # Down: 0, -100, -300 (deltas: -100, -200 -> accel = -100)
    a_down.update(200000)
    a_down.update(199900)
    sig_down = a_down.update(199700)
    assert sig_up == pytest.approx(-sig_down, abs=1e-9)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence_constant_accel() -> None:
    """Under constant acceleration, EMA should converge to that value."""
    alpha = PriceAccelerationAlpha()
    # constant accel = 20: deltas are 100, 120, 140, ... -> accel always 20
    base = 200000
    delta = 100
    accel = 20
    alpha.update(base)
    pos = base
    for i in range(1, 200):
        current_delta = delta + accel * (i - 1)
        pos += current_delta
        alpha.update(pos)
    assert alpha.get_signal() == pytest.approx(accel, abs=0.5)


def test_ema_third_step() -> None:
    """Verify EMA calculation on the third update (first real acceleration)."""
    alpha = PriceAccelerationAlpha()
    m1, m2, m3 = 200000.0, 200100.0, 200300.0
    alpha.update(m1)  # tick 0
    alpha.update(m2)  # tick 1: delta = 100
    sig = alpha.update(m3)  # tick 2: delta = 200, accel = 100
    # First EMA step: ema was 0, so ema += alpha_ema * (100 - 0)
    expected = _EMA_ALPHA_8 * 100.0
    assert sig == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs() -> None:
    """update() accepts keyword arguments."""
    alpha = PriceAccelerationAlpha()
    sig = alpha.update(mid_price=200000)
    assert sig == 0.0
    alpha.update(mid_price=200100)
    sig3 = alpha.update(mid_price=200300)
    assert sig3 != 0.0


def test_positional() -> None:
    """update() accepts positional arguments."""
    alpha = PriceAccelerationAlpha()
    sig = alpha.update(200000)
    assert sig == 0.0


def test_reset() -> None:
    """reset() clears all state; next update acts as first tick."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    alpha.update(200100)
    alpha.update(200300)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    sig = alpha.update(200000)
    assert sig == 0.0  # first tick after reset


def test_get_signal() -> None:
    """get_signal() returns last computed signal."""
    alpha = PriceAccelerationAlpha()
    alpha.update(200000)
    alpha.update(200100)
    ret = alpha.update(200300)
    assert alpha.get_signal() == ret


def test_bounded() -> None:
    """Signal should remain finite under random input."""
    import numpy as np

    alpha = PriceAccelerationAlpha()
    rng = np.random.default_rng(42)
    mids = rng.integers(190000, 210000, 200)
    for m in mids:
        sig = alpha.update(int(m))
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = PriceAccelerationAlpha()
    assert isinstance(alpha, AlphaProtocol)

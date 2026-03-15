"""Gate B correctness tests for GARCHVolAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.garch_vol.impl import (
    _EMA_ALPHA_8,
    _EMA_ALPHA_64,
    _EPSILON,
    ALPHA_CLASS,
    GARCHVolAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert GARCHVolAlpha().manifest.alpha_id == "garch_vol"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert GARCHVolAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = GARCHVolAlpha().manifest.data_fields
    assert "mid_price" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert GARCHVolAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert GARCHVolAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = GARCHVolAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is GARCHVolAlpha


# ---------------------------------------------------------------------------
# First update behavior
# ---------------------------------------------------------------------------


def test_first_update_zero() -> None:
    """First tick stores prev mid_price, returns 0 (no delta yet)."""
    alpha = GARCHVolAlpha()
    sig = alpha.update(100000)
    assert sig == 0.0


def test_initial_zero() -> None:
    """get_signal() before any update is zero."""
    alpha = GARCHVolAlpha()
    assert alpha.get_signal() == 0.0


def test_second_nonzero() -> None:
    """Second update with a different mid_price must produce non-zero signal."""
    alpha = GARCHVolAlpha()
    alpha.update(100000)
    sig = alpha.update(100100)
    assert sig != 0.0


# ---------------------------------------------------------------------------
# Signal direction: vol expansion vs contraction
# ---------------------------------------------------------------------------


def test_vol_expansion_positive() -> None:
    """After a calm period, a sudden large move should push signal positive."""
    alpha = GARCHVolAlpha()
    # Warm up with small moves to build long EMA
    base = 100000.0
    alpha.update(base)
    for i in range(100):
        alpha.update(base + (i % 2) * 10)  # tiny oscillation
    # Now inject a large move
    for _ in range(10):
        base += 500
        alpha.update(base)
    assert alpha.get_signal() > 0.0, "Vol expansion should produce positive signal"


def test_vol_contraction_negative() -> None:
    """After a volatile period, calming down should push signal negative."""
    alpha = GARCHVolAlpha()
    base = 100000.0
    alpha.update(base)
    # Start volatile
    for i in range(80):
        base += 500 * (1 if i % 2 == 0 else -1)
        alpha.update(base)
    # Then calm down
    calm = base
    for _ in range(200):
        alpha.update(calm)
        alpha.update(calm + 1)
    assert alpha.get_signal() < 0.0, "Vol contraction should produce negative signal"


# ---------------------------------------------------------------------------
# Signal boundary: finite under random input
# ---------------------------------------------------------------------------


def test_bounded() -> None:
    """Signal should remain finite under random input."""
    import numpy as np

    alpha = GARCHVolAlpha()
    rng = np.random.default_rng(42)
    prices = rng.integers(90000, 110000, 200)
    for p in prices:
        sig = alpha.update(int(p))
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_convergence_to_zero() -> None:
    """With constant step size, short/long EMAs converge, signal -> 0."""
    alpha = GARCHVolAlpha()
    base = 100000.0
    step = 50.0
    alpha.update(base)
    for i in range(1, 500):
        alpha.update(base + i * step)
    # Both EMAs converge to step^2, so ratio -> 1, signal -> 0
    assert abs(alpha.get_signal()) < 0.05


def test_ema_second_step() -> None:
    """Verify EMA calculation on the second update."""
    alpha = GARCHVolAlpha()
    m1, m2 = 100000.0, 100100.0
    alpha.update(m1)
    sig = alpha.update(m2)
    delta_sq = (m2 - m1) ** 2
    # Both EMAs start at 0, first step: ema += alpha * (delta_sq - 0)
    short_ema = _EMA_ALPHA_8 * delta_sq
    long_ema = _EMA_ALPHA_64 * delta_sq
    expected = short_ema / (long_ema + _EPSILON) - 1.0
    assert sig == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs() -> None:
    """update() accepts keyword arguments."""
    alpha = GARCHVolAlpha()
    sig = alpha.update(mid_price=100000)
    assert sig == 0.0  # first tick
    sig2 = alpha.update(mid_price=100100)
    assert sig2 != 0.0


def test_positional() -> None:
    """update() accepts positional arguments."""
    alpha = GARCHVolAlpha()
    sig = alpha.update(100000)
    assert sig == 0.0


def test_reset() -> None:
    """reset() clears all state; next update acts as first tick."""
    alpha = GARCHVolAlpha()
    alpha.update(100000)
    alpha.update(100100)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    sig = alpha.update(100000)
    assert sig == 0.0  # first tick after reset


def test_get_signal() -> None:
    """get_signal() returns last computed signal."""
    alpha = GARCHVolAlpha()
    alpha.update(100000)
    ret = alpha.update(100100)
    assert alpha.get_signal() == ret


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = GARCHVolAlpha()
    assert isinstance(alpha, AlphaProtocol)

"""Gate B correctness tests for OfiAccelerationAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.ofi_acceleration.impl import (
    ALPHA_CLASS,
    OfiAccelerationAlpha,
    _EMA_ALPHA_8,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert OfiAccelerationAlpha().manifest.alpha_id == "ofi_acceleration"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert OfiAccelerationAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = OfiAccelerationAlpha().manifest.data_fields
    assert "ofi_l1_raw" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert OfiAccelerationAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert OfiAccelerationAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = OfiAccelerationAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is OfiAccelerationAlpha


# ---------------------------------------------------------------------------
# Signal: initial conditions
# ---------------------------------------------------------------------------


def test_initial_zero() -> None:
    """First update returns 0 (no delta available yet)."""
    alpha = OfiAccelerationAlpha()
    sig = alpha.update(42.0)
    assert sig == 0.0


def test_first_update_zero() -> None:
    """Alias: first call always returns zero regardless of input."""
    alpha = OfiAccelerationAlpha()
    assert alpha.update(999.0) == 0.0


def test_second_update_nonzero() -> None:
    """Second update with different OFI must produce nonzero signal."""
    alpha = OfiAccelerationAlpha()
    alpha.update(10.0)
    sig = alpha.update(20.0)
    assert sig != 0.0


# ---------------------------------------------------------------------------
# Signal: direction
# ---------------------------------------------------------------------------


def test_increasing_ofi_positive() -> None:
    """Monotonically increasing OFI -> positive acceleration signal."""
    alpha = OfiAccelerationAlpha()
    for i in range(20):
        sig = alpha.update(float(i * 10))
    assert sig > 0.0


def test_decreasing_ofi_negative() -> None:
    """Monotonically decreasing OFI -> negative acceleration signal."""
    alpha = OfiAccelerationAlpha()
    for i in range(20):
        sig = alpha.update(float(200 - i * 10))
    assert sig < 0.0


def test_constant_ofi_zero() -> None:
    """Constant OFI -> delta = 0 -> signal converges to 0."""
    alpha = OfiAccelerationAlpha()
    for _ in range(100):
        alpha.update(50.0)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """With constant delta, EMA should converge to that delta."""
    alpha = OfiAccelerationAlpha()
    # Feed linearly increasing OFI: delta = 5.0 every tick.
    for i in range(200):
        alpha.update(float(i * 5))
    assert alpha.get_signal() == pytest.approx(5.0, abs=0.01)


def test_impulse_decay() -> None:
    """After a single impulse, signal should decay towards zero."""
    alpha = OfiAccelerationAlpha()
    alpha.update(0.0)
    alpha.update(100.0)  # big impulse: delta = 100
    impulse_sig = alpha.get_signal()
    assert impulse_sig > 0.0
    # Feed constant OFI -> delta = 0 -> EMA decays.
    for _ in range(50):
        alpha.update(100.0)
    assert abs(alpha.get_signal()) < abs(impulse_sig)


def test_ema_second_step_exact() -> None:
    """Second step: EMA = prev_ema + alpha*(delta - prev_ema)."""
    alpha = OfiAccelerationAlpha()
    alpha.update(10.0)  # first tick: stores prev, returns 0
    sig2 = alpha.update(30.0)  # delta = 20, EMA = 0 + alpha*20
    expected = _EMA_ALPHA_8 * 20.0
    assert sig2 == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Symmetry
# ---------------------------------------------------------------------------


def test_symmetric() -> None:
    """Symmetric positive/negative acceleration should yield opposite signs."""
    a1 = OfiAccelerationAlpha()
    a2 = OfiAccelerationAlpha()
    # a1: increasing
    a1.update(0.0)
    sig_up = a1.update(50.0)
    # a2: decreasing by same amount
    a2.update(50.0)
    sig_down = a2.update(0.0)
    assert sig_up == pytest.approx(-sig_down, abs=1e-9)


# ---------------------------------------------------------------------------
# Bounded behavior
# ---------------------------------------------------------------------------


def test_bounded() -> None:
    """Signal should not explode for reasonable inputs."""
    alpha = OfiAccelerationAlpha()
    import numpy as np

    rng = np.random.default_rng(42)
    ofi_values = rng.uniform(-1000, 1000, 500)
    for v in ofi_values:
        sig = alpha.update(float(v))
        # With random walk OFI, deltas are bounded by ~2000
        # EMA is a weighted average of those deltas, so bounded similarly
        assert abs(sig) < 3000.0


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_kwargs() -> None:
    alpha = OfiAccelerationAlpha()
    sig = alpha.update(ofi_l1_raw=10.0)
    assert sig == 0.0  # first tick


def test_update_accepts_positional() -> None:
    alpha = OfiAccelerationAlpha()
    sig = alpha.update(10.0)
    assert sig == 0.0  # first tick


def test_reset() -> None:
    alpha = OfiAccelerationAlpha()
    alpha.update(0.0)
    alpha.update(100.0)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should return 0.
    assert alpha.update(50.0) == 0.0


def test_get_signal() -> None:
    alpha = OfiAccelerationAlpha()
    assert alpha.get_signal() == 0.0
    alpha.update(0.0)
    alpha.update(10.0)
    assert alpha.get_signal() != 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = OfiAccelerationAlpha()
    assert isinstance(alpha, AlphaProtocol)

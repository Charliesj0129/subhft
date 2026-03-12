"""Gate B correctness tests for ImbalanceDivergenceAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.imbalance_divergence.impl import (
    _EMA_ALPHA_8,
    _PPM_SCALE,
    ALPHA_CLASS,
    ImbalanceDivergenceAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert ImbalanceDivergenceAlpha().manifest.alpha_id == "imbalance_divergence"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert ImbalanceDivergenceAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = ImbalanceDivergenceAlpha().manifest.data_fields
    assert "l1_imbalance_ppm" in fields
    assert "depth_imbalance_ppm" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert ImbalanceDivergenceAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ImbalanceDivergenceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ImbalanceDivergenceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ImbalanceDivergenceAlpha


# ---------------------------------------------------------------------------
# Signal: initial state
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Before any update, signal is 0."""
    alpha = ImbalanceDivergenceAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal: direction and sign
# ---------------------------------------------------------------------------


def test_l1_greater_than_depth_positive() -> None:
    """L1 imbalance > depth imbalance → positive divergence signal."""
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(500_000, 100_000)  # l1 > depth
    assert sig > 0.0


def test_depth_greater_than_l1_negative() -> None:
    """Depth imbalance > L1 imbalance → negative divergence signal."""
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(100_000, 500_000)  # depth > l1
    assert sig < 0.0


def test_equal_imbalances_zero() -> None:
    """Equal L1 and depth imbalance → divergence = 0, signal = 0."""
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(300_000, 300_000)
    assert sig == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_constant_input() -> None:
    """EMA should converge to the raw divergence given constant input."""
    alpha = ImbalanceDivergenceAlpha()
    l1, depth = 600_000.0, 200_000.0
    expected = (l1 - depth) / _PPM_SCALE
    for _ in range(100):
        alpha.update(l1, depth)
    assert alpha.get_signal() == pytest.approx(expected, abs=1e-6)


def test_ema_single_step_initializes_to_raw() -> None:
    """First update initializes EMA to the raw divergence (no prior)."""
    alpha = ImbalanceDivergenceAlpha()
    l1, depth = 800_000.0, 200_000.0
    expected = (l1 - depth) / _PPM_SCALE
    sig = alpha.update(l1, depth)
    assert sig == pytest.approx(expected, abs=1e-12)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = ImbalanceDivergenceAlpha()
    l1_1, d1 = 600_000.0, 200_000.0
    l1_2, d2 = 200_000.0, 600_000.0
    div1 = (l1_1 - d1) / _PPM_SCALE
    div2 = (l1_2 - d2) / _PPM_SCALE
    expected_ema2 = div1 + _EMA_ALPHA_8 * (div2 - div1)
    alpha.update(l1_1, d1)
    sig2 = alpha.update(l1_2, d2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-12)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = ImbalanceDivergenceAlpha()
    alpha.update(800_000, 100_000)
    alpha.reset()
    sig = alpha.update(300_000, 300_000)
    assert sig == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(l1_imbalance_ppm=500_000, depth_imbalance_ppm=100_000)
    expected = (500_000 - 100_000) / _PPM_SCALE
    assert sig == pytest.approx(expected, abs=1e-12)


def test_update_accepts_positional_args() -> None:
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(500_000, 100_000)
    expected = (500_000 - 100_000) / _PPM_SCALE
    assert sig == pytest.approx(expected, abs=1e-12)


def test_update_one_arg_raises() -> None:
    alpha = ImbalanceDivergenceAlpha()
    with pytest.raises(ValueError):
        alpha.update(100_000)


def test_get_signal_returns_last_update() -> None:
    alpha = ImbalanceDivergenceAlpha()
    sig = alpha.update(700_000, 200_000)
    assert alpha.get_signal() == sig


# ---------------------------------------------------------------------------
# Symmetry and boundedness
# ---------------------------------------------------------------------------


def test_symmetric_inputs_opposite_sign() -> None:
    """Swapping l1 and depth should give the exact negative signal."""
    a1 = ImbalanceDivergenceAlpha()
    a2 = ImbalanceDivergenceAlpha()
    s1 = a1.update(700_000, 300_000)
    s2 = a2.update(300_000, 700_000)
    assert s1 == pytest.approx(-s2, abs=1e-12)


def test_bounded_under_random_inputs() -> None:
    """Signal magnitude should stay bounded (ppm range [-1e6, 1e6] -> div in [-2, 2])."""
    alpha = ImbalanceDivergenceAlpha()
    rng = np.random.default_rng(42)
    l1_vals = rng.integers(-_PPM_SCALE, _PPM_SCALE + 1, size=200)
    depth_vals = rng.integers(-_PPM_SCALE, _PPM_SCALE + 1, size=200)
    for l1, depth in zip(l1_vals, depth_vals):
        sig = alpha.update(float(l1), float(depth))
        assert -2.0 <= sig <= 2.0


def test_stable_zero_on_constant_zero() -> None:
    """Feeding (0, 0) repeatedly should keep signal at 0."""
    alpha = ImbalanceDivergenceAlpha()
    for _ in range(50):
        alpha.update(0, 0)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ImbalanceDivergenceAlpha()
    assert isinstance(alpha, AlphaProtocol)

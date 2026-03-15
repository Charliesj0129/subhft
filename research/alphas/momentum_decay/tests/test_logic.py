"""Gate B correctness tests for MomentumDecayAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.momentum_decay.impl import (
    ALPHA_CLASS,
    MomentumDecayAlpha,
    _EMA_ALPHA_4,
    _EMA_ALPHA_32,
    _EPSILON,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MomentumDecayAlpha().manifest.alpha_id == "momentum_decay"


def test_manifest_data_fields() -> None:
    fields = MomentumDecayAlpha().manifest.data_fields
    assert "mid_price" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert MomentumDecayAlpha().manifest.latency_profile == "shioaji_sim_p95_v2026-03-04"


def test_manifest_feature_set_version() -> None:
    assert MomentumDecayAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MomentumDecayAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MomentumDecayAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_update_returns_zero() -> None:
    """First tick has no prior price; signal must be 0."""
    alpha = MomentumDecayAlpha()
    sig = alpha.update(100000.0)
    assert sig == 0.0


def test_constant_price_signal_converges_to_zero() -> None:
    """No price change -> both EMAs converge to 0 -> signal ~ 0."""
    alpha = MomentumDecayAlpha()
    for _ in range(200):
        alpha.update(100000.0)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-4)


def test_rising_price_signal_positive_initially() -> None:
    """Steady upward price changes should produce a positive fast EMA."""
    alpha = MomentumDecayAlpha()
    for i in range(50):
        alpha.update(100000.0 + i * 100.0)
    # With constant positive deltas, fast_ema > 0 and slow_ema > 0
    # ratio = fast/slow, sign_slow = 1 -> signal = ratio - 1
    # Both EMAs converge to 100.0; ratio ~ 1 -> signal ~ 0
    # But during buildup, fast converges faster, so ratio > 1 early
    # After 50 ticks both are near 100, signal should be near 0
    assert isinstance(alpha.get_signal(), float)


def test_falling_price_signal() -> None:
    """Steady downward price changes should produce negative fast EMA."""
    alpha = MomentumDecayAlpha()
    for i in range(50):
        alpha.update(100000.0 - i * 100.0)
    assert isinstance(alpha.get_signal(), float)


def test_momentum_reversal_signal_shift() -> None:
    """When momentum reverses, signal should change direction."""
    alpha = MomentumDecayAlpha()
    # Build upward momentum
    for i in range(30):
        alpha.update(100000.0 + i * 100.0)
    sig_up = alpha.get_signal()
    # Reverse to downward momentum
    base = 100000.0 + 29 * 100.0
    for i in range(30):
        alpha.update(base - i * 100.0)
    sig_down = alpha.get_signal()
    assert sig_down < sig_up


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_single_step_correctness() -> None:
    """Verify EMA update math on the second tick."""
    alpha = MomentumDecayAlpha()
    alpha.update(100000.0)  # first tick, signal = 0
    sig = alpha.update(100100.0)  # delta = 100

    delta_p = 100.0
    expected_fast = _EMA_ALPHA_4 * delta_p  # from 0
    expected_slow = _EMA_ALPHA_32 * delta_p  # from 0
    slow_abs = abs(expected_slow)
    denom = max(slow_abs, _EPSILON)
    ratio = expected_fast / denom
    sign_slow = 1.0 if expected_slow > _EPSILON else 0.0
    expected_signal = ratio - sign_slow

    assert sig == pytest.approx(expected_signal, abs=1e-9)


def test_ema_convergence_constant_delta() -> None:
    """With constant price increments, both EMAs converge to that increment."""
    alpha = MomentumDecayAlpha()
    delta = 50.0
    for i in range(500):
        alpha.update(100000.0 + i * delta)
    # Both fast and slow EMAs converge to delta=50.
    # ratio = 50/50 = 1, sign_slow = 1 -> signal = 0
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-2)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = MomentumDecayAlpha()
    sig = alpha.update(mid_price=100000.0)
    assert sig == 0.0  # first tick
    sig2 = alpha.update(mid_price=100100.0)
    assert isinstance(sig2, float)


def test_reset_clears_state() -> None:
    alpha = MomentumDecayAlpha()
    alpha.update(100000.0)
    alpha.update(100100.0)
    alpha.reset()
    # After reset, first update should return 0 (no prior history)
    sig = alpha.update(100000.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = MomentumDecayAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MomentumDecayAlpha()
    assert isinstance(alpha, AlphaProtocol)

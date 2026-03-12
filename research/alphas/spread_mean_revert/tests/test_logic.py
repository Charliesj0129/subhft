"""Gate B correctness tests for SpreadMeanRevertAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.spread_mean_revert.impl import (
    ALPHA_CLASS,
    SpreadMeanRevertAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert SpreadMeanRevertAlpha().manifest.alpha_id == "spread_mean_revert"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert SpreadMeanRevertAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = SpreadMeanRevertAlpha().manifest.data_fields
    assert "spread_scaled" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert SpreadMeanRevertAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert SpreadMeanRevertAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = SpreadMeanRevertAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is SpreadMeanRevertAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Before any update, signal is 0."""
    alpha = SpreadMeanRevertAlpha()
    assert alpha.get_signal() == 0.0


def test_wide_spread_negative_signal() -> None:
    """Spread above baseline -> deviation positive -> signal negative (fade)."""
    alpha = SpreadMeanRevertAlpha()
    # Warm up at baseline 100
    for _ in range(200):
        alpha.update(100)
    # Spike to wide spread
    for _ in range(20):
        alpha.update(200)
    assert alpha.get_signal() < 0.0


def test_narrow_spread_positive_signal() -> None:
    """Spread below baseline -> deviation negative -> signal positive (fade)."""
    alpha = SpreadMeanRevertAlpha()
    # Warm up at baseline 100
    for _ in range(200):
        alpha.update(100)
    # Drop to narrow spread
    for _ in range(20):
        alpha.update(50)
    assert alpha.get_signal() > 0.0


def test_ema_convergence() -> None:
    """With constant input, EMA64 converges to input and signal -> 0."""
    alpha = SpreadMeanRevertAlpha()
    for _ in range(2000):
        alpha.update(100)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_reset_clears_state() -> None:
    """After reset, signal is 0 and next update behaves like fresh instance."""
    alpha = SpreadMeanRevertAlpha()
    alpha.update(500)
    alpha.update(200)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # First update after reset should initialize EMA64 and return 0
    sig = alpha.update(100)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_matches_update_return() -> None:
    """get_signal() must return same value as last update()."""
    alpha = SpreadMeanRevertAlpha()
    for spread in [100, 120, 80, 150, 90]:
        ret = alpha.update(spread)
        assert ret == alpha.get_signal()


def test_update_accepts_kwargs() -> None:
    """update() works with keyword argument."""
    alpha = SpreadMeanRevertAlpha()
    sig = alpha.update(spread_scaled=100)
    assert isinstance(sig, float)


def test_update_accepts_positional() -> None:
    """update() works with a single positional argument."""
    alpha = SpreadMeanRevertAlpha()
    sig = alpha.update(100)
    assert isinstance(sig, float)


def test_zero_spread() -> None:
    """Zero spread_scaled is handled without error."""
    alpha = SpreadMeanRevertAlpha()
    sig = alpha.update(0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_symmetric_response() -> None:
    """Symmetric deviations from baseline produce opposite-sign signals."""
    alpha_up = SpreadMeanRevertAlpha()
    alpha_down = SpreadMeanRevertAlpha()
    baseline = 100
    delta = 50
    # Both warm up at baseline
    for _ in range(500):
        alpha_up.update(baseline)
        alpha_down.update(baseline)
    # One goes up, other goes down
    for _ in range(30):
        alpha_up.update(baseline + delta)
        alpha_down.update(baseline - delta)
    sig_up = alpha_up.get_signal()
    sig_down = alpha_down.get_signal()
    assert sig_up < 0.0  # wide spread -> negative
    assert sig_down > 0.0  # narrow spread -> positive
    # Asymmetry is expected due to normalized deviation denominator shifting
    # with EMA_64; verify they are at least in the same order of magnitude
    assert abs(sig_up) == pytest.approx(abs(sig_down), rel=0.5)


def test_signal_bounded() -> None:
    """Signal should stay within reasonable bounds for realistic inputs."""
    alpha = SpreadMeanRevertAlpha()
    import numpy as np

    rng = np.random.default_rng(42)
    spreads = rng.uniform(10, 200, 500)
    for s in spreads:
        sig = alpha.update(float(s))
        assert -10.0 <= sig <= 10.0  # generous bound for normalized deviation


def test_stable_spread_zero_signal() -> None:
    """Constant spread produces zero signal after convergence."""
    alpha = SpreadMeanRevertAlpha()
    for _ in range(2000):
        alpha.update(50)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_impulse_decay() -> None:
    """After a single impulse, signal should decay back toward zero."""
    alpha = SpreadMeanRevertAlpha()
    # Establish baseline
    for _ in range(500):
        alpha.update(100)
    # Impulse
    alpha.update(300)
    sig_after_impulse = abs(alpha.get_signal())
    # Decay
    for _ in range(200):
        alpha.update(100)
    sig_after_decay = abs(alpha.get_signal())
    assert sig_after_decay < sig_after_impulse


def test_multiple_updates_converge() -> None:
    """Two alphas fed same data converge to same signal regardless of init."""
    alpha = SpreadMeanRevertAlpha()
    data = [100, 110, 90, 105, 95, 120, 80, 100, 100, 100] * 50
    for s in data:
        alpha.update(s)
    sig = alpha.get_signal()
    # Second alpha, same data
    alpha2 = SpreadMeanRevertAlpha()
    for s in data:
        alpha2.update(s)
    assert sig == pytest.approx(alpha2.get_signal(), abs=1e-12)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = SpreadMeanRevertAlpha()
    assert isinstance(alpha, AlphaProtocol)

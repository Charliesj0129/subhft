"""Gate B correctness tests for AmihudIlliquidityAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.amihud_illiquidity.impl import (
    ALPHA_CLASS,
    AmihudIlliquidityAlpha,
    _EMA_ALPHA_16,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert AmihudIlliquidityAlpha().manifest.alpha_id == "amihud_illiquidity"


def test_manifest_data_fields() -> None:
    fields = AmihudIlliquidityAlpha().manifest.data_fields
    assert "mid_price" in fields
    assert "volume" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert AmihudIlliquidityAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert AmihudIlliquidityAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = AmihudIlliquidityAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is AmihudIlliquidityAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_signal_boundary() -> None:
    """Run 200 updates with random data; signal stays non-negative and finite."""
    alpha = AmihudIlliquidityAlpha()
    rng = np.random.default_rng(42)
    mids = rng.uniform(90, 110, 200)
    vols = rng.uniform(1, 1000, 200)
    for m, v in zip(mids, vols):
        sig = alpha.update(mid_price=float(m), volume=float(v))
        assert sig >= 0.0, "Illiquidity signal must be non-negative"
        assert np.isfinite(sig), "Signal must be finite"


def test_first_update_returns_zero() -> None:
    """First update initializes state — signal is 0."""
    alpha = AmihudIlliquidityAlpha()
    sig = alpha.update(mid_price=100.0, volume=500.0)
    assert sig == 0.0


def test_price_move_produces_positive_signal() -> None:
    """A price change with finite volume should produce positive signal."""
    alpha = AmihudIlliquidityAlpha()
    alpha.update(mid_price=100.0, volume=100.0)
    sig = alpha.update(mid_price=101.0, volume=100.0)
    assert sig > 0.0


def test_zero_volume_does_not_crash() -> None:
    """Zero volume guarded by epsilon — no crash or inf."""
    alpha = AmihudIlliquidityAlpha()
    alpha.update(mid_price=100.0, volume=0.0)
    sig = alpha.update(mid_price=101.0, volume=0.0)
    assert np.isfinite(sig)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """Constant input → EMA converges to the constant illiquidity ratio."""
    alpha = AmihudIlliquidityAlpha()
    mid, vol = 100.0, 500.0
    # First tick: initialization
    alpha.update(mid_price=mid, volume=vol)
    # Subsequent ticks: no price change → illiq = 0 → EMA → 0
    for _ in range(200):
        alpha.update(mid_price=mid, volume=vol)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


def test_ema_step_correctness() -> None:
    """Verify EMA update: ema += alpha * (raw - ema)."""
    alpha = AmihudIlliquidityAlpha()
    m1, v1 = 100.0, 200.0
    m2, v2 = 102.0, 200.0
    alpha.update(mid_price=m1, volume=v1)  # init, signal=0
    sig2 = alpha.update(mid_price=m2, volume=v2)
    # Expected: abs_ret = |102-100|/100 = 0.02, illiq = 0.02/200 = 0.0001
    # ema = 0 + alpha * (0.0001 - 0) = alpha * 0.0001
    expected = _EMA_ALPHA_16 * (0.02 / 200.0)
    assert sig2 == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs_acceptance() -> None:
    """update() accepts keyword arguments."""
    alpha = AmihudIlliquidityAlpha()
    sig = alpha.update(mid_price=100.0, volume=500.0)
    assert isinstance(sig, float)


def test_reset_clears_state() -> None:
    alpha = AmihudIlliquidityAlpha()
    alpha.update(mid_price=100.0, volume=100.0)
    alpha.update(mid_price=105.0, volume=100.0)
    alpha.reset()
    # After reset, first update should return 0 (re-initialization)
    sig = alpha.update(mid_price=100.0, volume=100.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = AmihudIlliquidityAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = AmihudIlliquidityAlpha()
    assert isinstance(alpha, AlphaProtocol)

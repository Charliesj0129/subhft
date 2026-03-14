"""Gate B correctness tests for AdverseMomentumAlpha (refs 131, 136)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.adverse_momentum.impl import (
    ALPHA_CLASS,
    AdverseMomentumAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert AdverseMomentumAlpha().manifest.alpha_id == "adverse_momentum"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert AdverseMomentumAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    refs = AdverseMomentumAlpha().manifest.paper_refs
    assert "131" in refs
    assert "136" in refs


def test_manifest_data_fields() -> None:
    fields = AdverseMomentumAlpha().manifest.data_fields
    assert "mid_price" in fields
    assert "ofi_l1_ema8" in fields
    assert "spread_scaled" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert AdverseMomentumAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert AdverseMomentumAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = AdverseMomentumAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is AdverseMomentumAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_zero_ofi_signal_zero() -> None:
    """When ofi=0 throughout, signed_residual=0 so signal stays ~0."""
    alpha = AdverseMomentumAlpha()
    for i in range(50):
        sig = alpha.update(100.0 + i * 0.1, 0.0, 10.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_perfect_prediction_zero_residual() -> None:
    """When delta_mid = beta * ofi exactly, residual is zero.

    Feed a constant ofi and constant delta_mid so beta converges;
    once converged, residual -> 0 and signal -> 0.
    """
    alpha = AdverseMomentumAlpha()
    mid = 100.0
    ofi_val = 5.0
    delta = 2.0  # constant delta_mid each tick
    for _ in range(500):
        mid += delta
        alpha.update(mid, ofi_val, 10.0)
    # After convergence, beta ~ delta/ofi = 0.4, residual ~ 0
    assert abs(alpha.get_signal()) < 0.05


def test_positive_ofi_excess_return_positive() -> None:
    """Buy OFI + excess return (delta_mid >> beta*ofi) -> positive signal."""
    alpha = AdverseMomentumAlpha()
    mid = 100.0
    # First, warm up beta with modest relationship
    for _ in range(100):
        mid += 0.5
        alpha.update(mid, 10.0, 10.0)
    # Now inject excess returns (large delta_mid relative to what beta predicts)
    for _ in range(100):
        mid += 5.0  # much larger than expected
        alpha.update(mid, 10.0, 10.0)
    assert alpha.get_signal() > 0.0


def test_signal_bounded() -> None:
    """Random fuzz: signal must always be in [-2, 2]."""
    alpha = AdverseMomentumAlpha()
    rng = np.random.default_rng(42)
    mids = np.cumsum(rng.normal(0, 10, 500)) + 1000.0
    ofis = rng.normal(0, 5, 500)
    spreads = rng.uniform(1, 20, 500)
    for m, o, s in zip(mids, ofis, spreads):
        sig = alpha.update(float(m), float(o), float(s))
        assert -2.0 <= sig <= 2.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """Constant signed_residual: residual_ema should converge to that value."""
    alpha = AdverseMomentumAlpha()
    mid = 100.0
    # Feed ticks with constant large excess return and constant positive OFI
    # so the signed residual converges to a positive constant.
    for _ in range(1000):
        mid += 10.0  # large constant return
        alpha.update(mid, 1.0, 10.0)
    sig = alpha.get_signal()
    # Signal should have converged close to the residual (clipped at 2.0 if large)
    assert sig > 0.0


def test_first_update_initializes() -> None:
    """First update should set _initialized and return 0.0 (delta_mid=0)."""
    alpha = AdverseMomentumAlpha()
    sig = alpha.update(100.0, 5.0, 10.0)
    # First tick: delta_mid=0 so residual=0 and signal=0
    assert sig == pytest.approx(0.0, abs=1e-9)
    assert alpha._initialized is True


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = AdverseMomentumAlpha()
    sig = alpha.update(mid_price=100.0, ofi_l1_ema8=5.0, spread_scaled=10.0)
    assert isinstance(sig, float)


def test_update_wrong_arg_count_raises() -> None:
    alpha = AdverseMomentumAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 5.0)  # only 2 positional, need 3


def test_reset_clears_state() -> None:
    alpha = AdverseMomentumAlpha()
    alpha.update(100.0, 5.0, 10.0)
    alpha.update(110.0, 3.0, 10.0)
    alpha.reset()
    # After reset, first update should behave like fresh instance
    sig = alpha.update(100.0, 0.0, 10.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_zero() -> None:
    alpha = AdverseMomentumAlpha()
    assert alpha.get_signal() == 0.0


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = AdverseMomentumAlpha()
    assert isinstance(alpha, AlphaProtocol)

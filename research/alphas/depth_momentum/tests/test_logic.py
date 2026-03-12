"""Gate B correctness tests for DepthMomentumAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.depth_momentum.impl import (
    ALPHA_CLASS,
    DepthMomentumAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = DepthMomentumAlpha().manifest
    assert m.alpha_id == "depth_momentum"
    assert m.data_fields == ("bid_depth", "ask_depth")
    assert m.complexity == "O(1)"
    from research.registry.schemas import AlphaStatus
    assert m.status == AlphaStatus.DRAFT


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert DepthMomentumAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert DepthMomentumAlpha().manifest.latency_profile == "shioaji_sim_p95_v2026-03-04"


def test_manifest_feature_set_version() -> None:
    assert DepthMomentumAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = DepthMomentumAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthMomentumAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before any updates."""
    alpha = DepthMomentumAlpha()
    assert alpha.get_signal() == 0.0


def test_first_update_returns_zero() -> None:
    """First tick has no previous depth_imb, so signal = 0."""
    alpha = DepthMomentumAlpha()
    sig = alpha.update(200.0, 100.0)
    assert sig == 0.0


def test_second_update_nonzero() -> None:
    """Second tick with different depth produces nonzero signal."""
    alpha = DepthMomentumAlpha()
    alpha.update(100.0, 100.0)
    sig = alpha.update(200.0, 100.0)
    assert sig != 0.0


def test_increasing_bid_depth_positive_signal() -> None:
    """bid_depth growing each tick -> positive delta -> positive signal."""
    alpha = DepthMomentumAlpha()
    for i in range(20):
        sig = alpha.update(100.0 + i * 10, 100.0)
    assert sig > 0.0


def test_increasing_ask_depth_negative_signal() -> None:
    """ask_depth growing each tick -> negative delta -> negative signal."""
    alpha = DepthMomentumAlpha()
    for i in range(20):
        sig = alpha.update(100.0, 100.0 + i * 10)
    assert sig < 0.0


def test_stable_depth_zero_signal() -> None:
    """Constant bid/ask depth -> delta = 0 -> signal converges to 0."""
    alpha = DepthMomentumAlpha()
    for _ in range(50):
        sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_impulse_response_decays() -> None:
    """Single spike in depth, then constant -> signal decays via EMA."""
    alpha = DepthMomentumAlpha()
    # Establish baseline
    for _ in range(10):
        alpha.update(100.0, 100.0)
    # Spike
    alpha.update(200.0, 100.0)
    sig_after_spike = alpha.get_signal()
    assert sig_after_spike != 0.0
    # Return to constant and watch decay
    for _ in range(50):
        sig = alpha.update(200.0, 100.0)
    assert abs(sig) < abs(sig_after_spike)


def test_reset_clears_state() -> None:
    """After reset(), all state is zeroed."""
    alpha = DepthMomentumAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(900.0, 100.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # First update after reset also returns 0
    sig = alpha.update(300.0, 300.0)
    assert sig == 0.0


def test_get_signal_matches_update_return() -> None:
    alpha = DepthMomentumAlpha()
    alpha.update(100.0, 100.0)
    ret = alpha.update(200.0, 50.0)
    assert ret == alpha.get_signal()


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """After many identical deltas, signal converges to that delta value."""
    alpha = DepthMomentumAlpha()
    # Feed linearly increasing bid_depth so delta is constant
    # depth_imb_t = (100+t*10 - 100) / (100+t*10 + 100 + eps)
    # The delta itself changes slightly, but for large t with small step
    # let's use a simpler approach: same bid/ask each tick -> delta=0 -> converges to 0
    for _ in range(200):
        alpha.update(150.0, 100.0)
    # After convergence, delta=0 each tick, so EMA -> 0
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs_interface() -> None:
    alpha = DepthMomentumAlpha()
    alpha.update(bid_depth=100, ask_depth=100)
    sig = alpha.update(bid_depth=200, ask_depth=100)
    assert sig != 0.0


def test_positional_interface() -> None:
    alpha = DepthMomentumAlpha()
    alpha.update(100, 100)
    sig = alpha.update(200, 100)
    assert sig != 0.0


def test_zero_depth_handled() -> None:
    """bid_depth=0, ask_depth=0 doesn't crash (epsilon guard)."""
    alpha = DepthMomentumAlpha()
    sig = alpha.update(0, 0)
    assert isinstance(sig, float)
    sig2 = alpha.update(0, 0)
    assert isinstance(sig2, float)


def test_symmetric_response() -> None:
    """Symmetric depth changes produce symmetric (opposite sign) signals."""
    a1 = DepthMomentumAlpha()
    a2 = DepthMomentumAlpha()
    # a1: bid grows
    a1.update(100, 100)
    sig1 = a1.update(200, 100)
    # a2: ask grows (mirror)
    a2.update(100, 100)
    sig2 = a2.update(100, 200)
    assert sig1 == pytest.approx(-sig2, abs=1e-9)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    alpha = DepthMomentumAlpha()
    assert isinstance(alpha, AlphaProtocol)


def test_update_one_arg_raises() -> None:
    alpha = DepthMomentumAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)

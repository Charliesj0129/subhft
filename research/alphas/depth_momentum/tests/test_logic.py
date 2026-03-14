"""Unit tests for depth_momentum signal logic (>=20 tests).

Tests cover manifest integrity, EMA convergence, signal direction,
state management, and API compatibility.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.depth_momentum.impl import (
    _A16,
    _A64,
    _MANIFEST,
    DepthMomentumAlpha,
)
from research.registry.schemas import VALID_ROLES, VALID_SKILLS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alpha() -> DepthMomentumAlpha:
    a = DepthMomentumAlpha()
    a.reset()
    return a


def _warmup(
    alpha: DepthMomentumAlpha,
    n: int = 200,
    bid: float = 100.0,
    ask: float = 100.0,
) -> None:
    for _ in range(n):
        alpha.update(bid, ask)


# ---------------------------------------------------------------------------
# 1-8: Manifest integrity
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert _MANIFEST.alpha_id == "depth_momentum"


def test_manifest_hypothesis_nonempty() -> None:
    assert len(_MANIFEST.hypothesis) > 0


def test_manifest_formula_nonempty() -> None:
    assert len(_MANIFEST.formula) > 0


def test_manifest_paper_refs() -> None:
    assert "013" in _MANIFEST.paper_refs


def test_manifest_complexity() -> None:
    assert _MANIFEST.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    assert _MANIFEST.latency_profile is not None
    assert "shioaji" in _MANIFEST.latency_profile


def test_manifest_feature_set_version() -> None:
    assert _MANIFEST.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    bad_roles = set(_MANIFEST.roles_used) - VALID_ROLES
    bad_skills = set(_MANIFEST.skills_used) - VALID_SKILLS
    assert not bad_roles, f"Unknown roles: {bad_roles}"
    assert not bad_skills, f"Unknown skills: {bad_skills}"


# ---------------------------------------------------------------------------
# 9: Protocol conformance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    alpha = _make_alpha()
    assert hasattr(alpha, "manifest")
    assert hasattr(alpha, "update")
    assert hasattr(alpha, "reset")
    assert hasattr(alpha, "get_signal")
    assert callable(alpha.update)
    assert callable(alpha.reset)
    assert callable(alpha.get_signal)


# ---------------------------------------------------------------------------
# 10-13: Signal behavior
# ---------------------------------------------------------------------------


def test_equal_inputs_signal_near_zero() -> None:
    """Symmetric constant input -> delta=0 every tick -> signal near 0."""
    alpha = _make_alpha()
    _warmup(alpha, 300)
    assert abs(alpha.get_signal()) < 0.05


def test_bid_dominance_positive_signal() -> None:
    """Increasing total depth (bid rising) -> positive momentum -> positive signal."""
    alpha = _make_alpha()
    # Start with baseline, then increase bid each tick
    for i in range(200):
        alpha.update(100.0 + i * 1.0, 100.0)
    assert alpha.get_signal() > 0.0, f"Expected positive signal, got {alpha.get_signal()}"


def test_ask_dominance_negative_signal() -> None:
    """Decreasing total depth (ask falling) -> negative momentum -> negative signal."""
    alpha = _make_alpha()
    # Start high, then decrease total depth
    for i in range(200):
        alpha.update(200.0 - i * 0.5, 200.0 - i * 0.5)
    assert alpha.get_signal() < 0.0, f"Expected negative signal, got {alpha.get_signal()}"


def test_signal_bounded() -> None:
    """Signal must stay in [-2.0, 2.0] for random inputs."""
    rng = np.random.default_rng(42)
    alpha = _make_alpha()
    for _ in range(1000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        s = alpha.update(bid, ask)
        assert -2.0 <= s <= 2.0, f"Signal out of bounds: {s}"


# ---------------------------------------------------------------------------
# 14: EMA convergence
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """After sufficient warmup with constant increasing delta, EMAs stabilize."""
    alpha = _make_alpha()
    # Feed constant delta by linearly increasing total depth
    for i in range(800):
        alpha.update(100.0 + i * 0.1, 100.0)
    s1 = alpha.update(100.0 + 800 * 0.1, 100.0)
    s2 = alpha.update(100.0 + 801 * 0.1, 100.0)
    # After long warmup, signals should be very close (constant delta)
    assert abs(s2 - s1) < 0.01, f"EMA did not converge: s1={s1}, s2={s2}"


# ---------------------------------------------------------------------------
# 15-17: API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    r1 = alpha1.update(150.0, 100.0)
    r2 = alpha2.update(bid_qty=150.0, ask_qty=100.0)
    assert abs(r1 - r2) < 1e-12


def test_update_accepts_bids_asks_arrays() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    r1 = alpha1.update(150.0, 100.0)
    bids = np.array([[50000, 150.0]])
    asks = np.array([[50100, 100.0]])
    r2 = alpha2.update(bids=bids, asks=asks)
    assert abs(r1 - r2) < 1e-12


def test_update_accepts_positional_args() -> None:
    alpha = _make_alpha()
    r = alpha.update(150.0, 100.0)
    assert math.isfinite(r)


# ---------------------------------------------------------------------------
# 18-19: State management
# ---------------------------------------------------------------------------


def test_get_signal_matches_last_update() -> None:
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)
    result = alpha.update(150.0, 100.0)
    assert result == alpha.get_signal()


def test_reset_clears_state() -> None:
    alpha = _make_alpha()
    for i in range(50):
        alpha.update(100.0 + i, 100.0)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha._prev_total == 0.0
    assert alpha._momentum_ema == 0.0
    assert alpha._baseline_ema == 0.0
    assert alpha._signal == 0.0
    assert not alpha._initialized


# ---------------------------------------------------------------------------
# 20: Single arg raises
# ---------------------------------------------------------------------------


def test_single_arg_raises() -> None:
    alpha = _make_alpha()
    with pytest.raises(ValueError, match="requires 2 positional args"):
        alpha.update(100.0)


# ---------------------------------------------------------------------------
# 21: EMA coefficient values
# ---------------------------------------------------------------------------


def test_ema_coefficients() -> None:
    assert abs(_A16 - (1.0 - math.exp(-1.0 / 16.0))) < 1e-12
    assert abs(_A64 - (1.0 - math.exp(-1.0 / 64.0))) < 1e-12


# ---------------------------------------------------------------------------
# 22: Manifest property returns cached instance
# ---------------------------------------------------------------------------


def test_manifest_property_returns_cached_manifest() -> None:
    alpha = _make_alpha()
    assert alpha.manifest is _MANIFEST
    assert alpha.manifest.alpha_id == "depth_momentum"

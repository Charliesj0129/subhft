"""Gate B correctness tests for DepthVelocityDiffAlpha (ref 039)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.depth_velocity_diff.impl import (
    _A8,
    _A32,
    _EPSILON,
    ALPHA_CLASS,
    DepthVelocityDiffAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert DepthVelocityDiffAlpha().manifest.alpha_id == "depth_velocity_diff"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert DepthVelocityDiffAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_039() -> None:
    assert "039" in DepthVelocityDiffAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = DepthVelocityDiffAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    assert DepthVelocityDiffAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert DepthVelocityDiffAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = DepthVelocityDiffAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthVelocityDiffAlpha


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = DepthVelocityDiffAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# First tick behaviour
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First tick initializes prev values; signal should be 0."""
    alpha = DepthVelocityDiffAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = DepthVelocityDiffAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_bid_depth_increasing_gives_positive_signal() -> None:
    """Bid depth growing faster than ask -> positive signal."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)  # init
    for _ in range(30):
        alpha.update(200.0, 100.0)  # bid grows, ask constant
    assert alpha.get_signal() > 0.0


def test_ask_depth_increasing_gives_negative_signal() -> None:
    """Ask depth growing faster than bid -> negative signal."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)  # init
    for _ in range(30):
        alpha.update(100.0, 200.0)  # ask grows, bid constant
    assert alpha.get_signal() < 0.0


def test_symmetric_change_signal_zero() -> None:
    """Equal depth changes cancel out -> signal near zero."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)
    # Both grow by same amount each tick
    for i in range(30):
        v = 100.0 + i * 10.0
        alpha.update(v, v)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Signal bounds
# ---------------------------------------------------------------------------


def test_signal_bounded_minus2_to_plus2() -> None:
    """Signal must stay in [-2, 2] for arbitrary inputs."""
    alpha = DepthVelocityDiffAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 300)
    asks = rng.uniform(0, 1000, 300)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 <= sig <= 2.0, f"Signal {sig} out of bounds"


def test_extreme_bid_growth_clips_at_plus2() -> None:
    """Extreme asymmetric bid growth should clip at +2."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(0.0, 1000.0)
    for _ in range(100):
        alpha.update(1e6, 0.0)  # massive bid growth
    assert alpha.get_signal() <= 2.0


def test_extreme_ask_growth_clips_at_minus2() -> None:
    """Extreme asymmetric ask growth should clip at -2."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(1000.0, 0.0)
    for _ in range(100):
        alpha.update(0.0, 1e6)  # massive ask growth
    assert alpha.get_signal() >= -2.0


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_convergence_constant_diff() -> None:
    """With a constant diff pattern, EMA_8 / EMA_32 should converge."""
    alpha = DepthVelocityDiffAlpha()
    # Init tick
    alpha.update(100.0, 100.0)
    # Constant step: bid +10 each tick, ask unchanged -> d_bid=10, d_ask=0, diff=10
    prev_bid = 100.0
    for _ in range(200):
        prev_bid += 10.0
        alpha.update(prev_bid, 100.0)
    # After convergence, diff=10 every tick, |diff|=10
    # EMA_8(10) -> 10, EMA_32(10) -> 10, ratio = 1.0
    assert alpha.get_signal() == pytest.approx(1.0, abs=0.05)


def test_ema_second_step_manual_check() -> None:
    """Verify EMA update formula on second tick."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)  # init: prev=(100, 100)
    sig = alpha.update(200.0, 100.0)  # d_bid=100, d_ask=0, diff=100

    # After first real tick:
    # _diff_ema = 0 + _A8 * (100 - 0) = _A8 * 100
    # _abs_diff_baseline = 0 + _A32 * (100 - 0) = _A32 * 100
    expected_ema = _A8 * 100.0
    expected_base = _A32 * 100.0
    expected_sig = max(-2.0, min(2.0, expected_ema / max(expected_base, _EPSILON)))
    assert sig == pytest.approx(expected_sig, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = DepthVelocityDiffAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)  # first tick -> 0


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = DepthVelocityDiffAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == pytest.approx(0.0, abs=1e-9)  # first tick -> 0


def test_update_one_arg_raises() -> None:
    alpha = DepthVelocityDiffAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)
    alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, first update should be init tick -> 0
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_has_slots() -> None:
    """Allocator Law: class must use __slots__."""
    assert hasattr(DepthVelocityDiffAlpha, "__slots__")
    alpha = DepthVelocityDiffAlpha()
    with pytest.raises(AttributeError):
        alpha._nonexistent_attr = 42  # type: ignore[attr-defined]

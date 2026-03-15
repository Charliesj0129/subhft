"""Gate B correctness tests for DepthRatioAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.depth_ratio.impl import (
    _EMA_ALPHA_8,
    _EPSILON,
    ALPHA_CLASS,
    DepthRatioAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert DepthRatioAlpha().manifest.alpha_id == "depth_ratio"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert DepthRatioAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = DepthRatioAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert DepthRatioAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert DepthRatioAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = DepthRatioAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthRatioAlpha


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Before any update, signal is 0."""
    alpha = DepthRatioAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_bid_dominant_positive() -> None:
    """Bid qty > ask qty -> positive signal."""
    alpha = DepthRatioAlpha()
    for _ in range(20):
        alpha.update(500.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_ask_dominant_negative() -> None:
    """Ask qty > bid qty -> negative signal."""
    alpha = DepthRatioAlpha()
    for _ in range(20):
        alpha.update(100.0, 500.0)
    assert alpha.get_signal() < 0.0


def test_equal_depth_zero() -> None:
    """Equal depths -> log(1) = 0 -> signal = 0."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(200.0, 200.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_depth_both() -> None:
    """Both depths zero -> log(eps/eps) = 0."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(0.0, 0.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_zero_bid_qty() -> None:
    """Zero bid qty -> log(eps/ask) -> negative signal."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(0.0, 100.0)
    assert sig < 0.0


def test_zero_ask_qty() -> None:
    """Zero ask qty -> log(bid/eps) -> positive signal."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(100.0, 0.0)
    assert sig > 0.0


def test_large_ratio() -> None:
    """Very large depth ratio produces finite signal (log compression)."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(1_000_000.0, 1.0)
    assert math.isfinite(sig)
    assert sig > 0.0
    # log(1e6) ~ 13.8, so signal should be roughly that
    assert sig == pytest.approx(math.log(1_000_000.0), abs=0.1)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """EMA should converge to the raw log ratio given constant input."""
    alpha = DepthRatioAlpha()
    bid, ask = 300.0, 100.0
    expected = math.log(300.0 / 100.0)
    for _ in range(200):
        alpha.update(bid, ask)
    assert alpha.get_signal() == pytest.approx(expected, abs=1e-4)


def test_ema_single_step_initializes() -> None:
    """First update initializes EMA to the raw log ratio."""
    alpha = DepthRatioAlpha()
    bid, ask = 400.0, 100.0
    expected = math.log(400.0 / 100.0)
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = DepthRatioAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    lr1 = math.log(300.0 / 100.0)
    lr2 = math.log(100.0 / 300.0)
    expected_ema2 = lr1 + _EMA_ALPHA_8 * (lr2 - lr1)
    alpha.update(b1, a1)
    sig2 = alpha.update(b2, a2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-9)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = DepthRatioAlpha()
    alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, equal depths should give 0
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_get_signal() -> None:
    """get_signal() returns last computed signal."""
    alpha = DepthRatioAlpha()
    ret = alpha.update(200.0, 100.0)
    assert alpha.get_signal() == ret


def test_update_kwargs() -> None:
    alpha = DepthRatioAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    expected = math.log(200.0 / 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_positional() -> None:
    alpha = DepthRatioAlpha()
    sig = alpha.update(200.0, 100.0)
    expected = math.log(200.0 / 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_one_arg_raises() -> None:
    alpha = DepthRatioAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


# ---------------------------------------------------------------------------
# Log smoothness property
# ---------------------------------------------------------------------------


def test_log_smoothness() -> None:
    """log(2) ~ 0.69 < 1.0 -- log is smoother than linear ratio."""
    alpha = DepthRatioAlpha()
    sig = alpha.update(200.0, 100.0)
    assert sig == pytest.approx(math.log(2.0), abs=1e-9)
    assert sig < 1.0


# ---------------------------------------------------------------------------
# Symmetry
# ---------------------------------------------------------------------------


def test_symmetric() -> None:
    """Swapping bid/ask should negate the signal."""
    a1 = DepthRatioAlpha()
    a2 = DepthRatioAlpha()
    s1 = a1.update(300.0, 100.0)
    s2 = a2.update(100.0, 300.0)
    assert s1 == pytest.approx(-s2, abs=1e-9)


# ---------------------------------------------------------------------------
# Bounded growth under random inputs
# ---------------------------------------------------------------------------


def test_bounded_random() -> None:
    """Signal stays finite under random inputs."""
    alpha = DepthRatioAlpha()
    rng = np.random.default_rng(42)
    depths = rng.uniform(0, 1000, (200, 2))
    for row in depths:
        sig = alpha.update(float(row[0]), float(row[1]))
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = DepthRatioAlpha()
    assert isinstance(alpha, AlphaProtocol)

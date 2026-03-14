"""Gate B logic tests for SpreadExcessToxicityAlpha."""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.append(os.getcwd())

from research.alphas.spread_excess_toxicity.impl import (
    ALPHA_CLASS,
    SpreadExcessToxicityAlpha,
)
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    alpha = SpreadExcessToxicityAlpha()
    m = alpha.manifest
    assert isinstance(m, AlphaManifest)
    assert m.alpha_id == "spread_excess_toxicity"
    assert m.paper_refs == ("131",)
    assert m.data_fields == ("bid_qty", "ask_qty", "spread_scaled", "ofi_l1_ema8")
    assert m.complexity == "O(1)"
    assert m.status == AlphaStatus.DRAFT
    assert m.tier == AlphaTier.TIER_2
    assert m.rust_module is None
    assert m.latency_profile == "shioaji_sim_p95_v2026-03-04"
    assert m.roles_used == ("planner", "code-reviewer")
    assert m.skills_used == ("iterative-retrieval", "validation-gate")
    assert m.feature_set_version == "lob_shared_v1"
    assert ALPHA_CLASS is SpreadExcessToxicityAlpha


# ---------------------------------------------------------------------------
# Basic signal behaviour
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    alpha = SpreadExcessToxicityAlpha()
    assert alpha.get_signal() == 0.0


def test_single_update_returns_float() -> None:
    alpha = SpreadExcessToxicityAlpha()
    sig = alpha.update(100, 100, 500, 1.0)
    assert isinstance(sig, float)


def test_spread_at_baseline_zero_signal() -> None:
    """When spread equals its baseline the excess is 0, so signal ~ 0."""
    alpha = SpreadExcessToxicityAlpha()
    # Feed constant spread so EMA converges to that value.
    for _ in range(500):
        alpha.update(100, 50, 500, 1.0)
    sig = alpha.get_signal()
    assert abs(sig) < 0.01, f"Expected ~0 when spread==baseline, got {sig}"


def test_spread_above_baseline_positive_excess() -> None:
    """Wider spread than baseline produces positive raw excess."""
    alpha = SpreadExcessToxicityAlpha()
    # Warm up baseline at 500
    for _ in range(300):
        alpha.update(100, 50, 500, 1.0)
    # Sudden spread widening to 1000 with positive ofi
    for _ in range(20):
        sig = alpha.update(100, 50, 1000, 1.0)
    assert sig > 0.0, f"Expected positive signal on spread widening, got {sig}"


def test_spread_below_baseline_negative_excess() -> None:
    """Tighter spread than baseline produces negative excess -> low toxicity."""
    alpha = SpreadExcessToxicityAlpha()
    # Warm up baseline at 500
    for _ in range(300):
        alpha.update(100, 50, 500, 1.0)
    # Spread narrows to 200 with positive ofi — hold long enough for EMA-8
    # to converge past the prior residual.
    for _ in range(100):
        sig = alpha.update(100, 50, 200, 1.0)
    # Excess is negative (tighter than baseline), so raw is negative
    # Signal should be negative (low toxicity, follows negative excess)
    assert sig < 0.0, f"Expected negative signal on spread narrowing, got {sig}"


# ---------------------------------------------------------------------------
# Directional tests
# ---------------------------------------------------------------------------


def test_positive_ofi_positive_signal() -> None:
    alpha = SpreadExcessToxicityAlpha()
    for _ in range(300):
        alpha.update(100, 50, 500, 1.0)
    for _ in range(30):
        sig = alpha.update(100, 50, 1000, 5.0)
    assert sig > 0.0


def test_negative_ofi_negative_signal() -> None:
    alpha = SpreadExcessToxicityAlpha()
    for _ in range(300):
        alpha.update(100, 50, 500, -1.0)
    for _ in range(30):
        sig = alpha.update(100, 50, 1000, -5.0)
    assert sig < 0.0


def test_zero_ofi_zero_signal() -> None:
    alpha = SpreadExcessToxicityAlpha()
    for _ in range(100):
        sig = alpha.update(100, 50, 500, 0.0)
    assert sig == 0.0


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------


def test_signal_clipped_at_bounds() -> None:
    alpha = SpreadExcessToxicityAlpha()
    # Drive spread_excess very high with huge spread vs small baseline
    for _ in range(200):
        sig = alpha.update(1000, 1, 100000, 100.0)
    assert -2.0 <= sig <= 2.0
    # Also check negative direction
    alpha2 = SpreadExcessToxicityAlpha()
    for _ in range(200):
        sig2 = alpha2.update(1000, 1, 100000, -100.0)
    assert -2.0 <= sig2 <= 2.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_quantities_no_crash() -> None:
    alpha = SpreadExcessToxicityAlpha()
    sig = alpha.update(0, 0, 500, 1.0)
    assert math.isfinite(sig)


def test_reset_clears_state() -> None:
    alpha = SpreadExcessToxicityAlpha()
    for _ in range(100):
        alpha.update(100, 50, 500, 1.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, behaves like a fresh instance
    fresh = SpreadExcessToxicityAlpha()
    sig1 = alpha.update(100, 50, 500, 1.0)
    sig2 = fresh.update(100, 50, 500, 1.0)
    assert sig1 == sig2


def test_get_signal_matches_update() -> None:
    alpha = SpreadExcessToxicityAlpha()
    ret = alpha.update(100, 50, 500, 1.0)
    assert ret == alpha.get_signal()


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """After constant input, spread_excess -> 0 and signal -> 0."""
    alpha = SpreadExcessToxicityAlpha()
    for _ in range(1000):
        sig = alpha.update(100, 50, 500, 1.0)
    assert abs(sig) < 0.01, f"Expected convergence to ~0, got {sig}"


# ---------------------------------------------------------------------------
# Call conventions
# ---------------------------------------------------------------------------


def test_keyword_args() -> None:
    alpha = SpreadExcessToxicityAlpha()
    sig = alpha.update(
        bid_qty=100, ask_qty=50, spread_scaled=500, ofi_l1_ema8=1.0
    )
    assert isinstance(sig, float)


def test_positional_args() -> None:
    alpha = SpreadExcessToxicityAlpha()
    sig = alpha.update(100, 50, 500, 1.0)
    assert isinstance(sig, float)


def test_wrong_positional_count_raises() -> None:
    alpha = SpreadExcessToxicityAlpha()
    with pytest.raises(ValueError, match="4 positional args"):
        alpha.update(100, 50, 500)

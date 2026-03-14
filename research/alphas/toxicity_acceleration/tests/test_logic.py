"""Gate B correctness tests for ToxicityAccelerationAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_acceleration.impl import (
    ALPHA_CLASS,
    ToxicityAccelerationAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = ToxicityAccelerationAlpha().manifest
    assert m.alpha_id == "toxicity_acceleration"
    assert m.data_fields == ("bid_qty", "ask_qty", "spread_scaled", "ofi_l1_ema8")
    assert m.paper_refs == ("129", "132")
    assert m.complexity == "O(1)"
    assert m.latency_profile == "shioaji_sim_p95_v2026-03-04"
    assert m.feature_set_version == "lob_shared_v1"
    from research.registry.schemas import AlphaStatus, AlphaTier

    assert m.status == AlphaStatus.DRAFT
    assert m.tier == AlphaTier.TIER_2


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ToxicityAccelerationAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ToxicityAccelerationAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = ToxicityAccelerationAlpha()
    assert alpha.get_signal() == 0.0


def test_single_update_returns_float() -> None:
    """A single update returns a float value."""
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 1.0)
    assert isinstance(sig, float)


def test_positive_acceleration_positive_ofi() -> None:
    """Sudden toxicity spike with positive OFI -> positive signal."""
    alpha = ToxicityAccelerationAlpha()
    # Establish low-toxicity baseline (balanced book, narrow spread)
    for _ in range(100):
        alpha.update(500.0, 500.0, 50.0, 0.5)
    # Now introduce high toxicity (extreme imbalance + wide spread)
    for _ in range(10):
        sig = alpha.update(1000.0, 10.0, 200.0, 1.0)
    # tox_fast should exceed tox_slow => positive acceleration
    assert sig > 0.0


def test_negative_acceleration_negative_ofi() -> None:
    """Sudden toxicity spike with negative OFI -> negative signal."""
    alpha = ToxicityAccelerationAlpha()
    for _ in range(100):
        alpha.update(500.0, 500.0, 50.0, -0.5)
    for _ in range(10):
        sig = alpha.update(10.0, 1000.0, 200.0, -1.0)
    assert sig < 0.0


def test_zero_ofi_zero_signal() -> None:
    """When ofi_l1_ema8 is exactly 0, signal is 0."""
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 0.0)
    assert sig == 0.0


def test_no_acceleration_low_signal() -> None:
    """After convergence, tox_fast ~ tox_slow => acceleration ~ 0."""
    alpha = ToxicityAccelerationAlpha()
    for _ in range(300):
        alpha.update(500.0, 100.0, 50.0, 1.0)
    sig = alpha.get_signal()
    # After convergence, fast and slow EMAs are nearly equal
    assert abs(sig) < 0.01


def test_signal_clipped_at_bounds() -> None:
    """Signal must be in [-2, 2]."""
    alpha = ToxicityAccelerationAlpha()
    # Establish zero-toxicity baseline
    for _ in range(200):
        alpha.update(500.0, 500.0, 50.0, 1.0)
    # Sudden extreme toxicity spike to force large acceleration
    for _ in range(5):
        sig = alpha.update(10000.0, 1.0, 5000.0, 1.0)
    assert -2.0 <= sig <= 2.0


def test_zero_quantities_no_crash() -> None:
    """bid_qty=0, ask_qty=0 doesn't crash."""
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(0.0, 0.0, 50.0, 0.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_reset_clears_state() -> None:
    alpha = ToxicityAccelerationAlpha()
    alpha.update(800.0, 100.0, 100.0, 1.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should equal fresh instance
    fresh = ToxicityAccelerationAlpha()
    s1 = alpha.update(300.0, 300.0, 50.0, 0.5)
    s2 = fresh.update(300.0, 300.0, 50.0, 0.5)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_get_signal_matches_update() -> None:
    alpha = ToxicityAccelerationAlpha()
    ret = alpha.update(500.0, 100.0, 80.0, 1.0)
    assert ret == alpha.get_signal()


def test_ema_convergence() -> None:
    """After 200+ constant ticks, tox_fast ~ tox_slow => acceleration ~ 0."""
    alpha = ToxicityAccelerationAlpha()
    for _ in range(300):
        alpha.update(300.0, 100.0, 50.0, 1.0)
    sig_a = alpha.get_signal()
    for _ in range(100):
        alpha.update(300.0, 100.0, 50.0, 1.0)
    sig_b = alpha.get_signal()
    assert sig_a == pytest.approx(sig_b, abs=1e-6)


def test_keyword_args() -> None:
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(
        bid_qty=500.0,
        ask_qty=100.0,
        spread_scaled=50.0,
        ofi_l1_ema8=1.0,
    )
    assert isinstance(sig, float)


def test_positional_args() -> None:
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 1.0)
    assert isinstance(sig, float)


def test_wrong_positional_count_raises() -> None:
    alpha = ToxicityAccelerationAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0, 300.0)


def test_spread_zero_no_crash() -> None:
    """spread_scaled=0 doesn't crash."""
    alpha = ToxicityAccelerationAlpha()
    sig = alpha.update(500.0, 100.0, 0.0, 1.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)

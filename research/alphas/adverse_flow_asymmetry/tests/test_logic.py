"""Gate B correctness tests for AdverseFlowAsymmetryAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.adverse_flow_asymmetry.impl import (
    ALPHA_CLASS,
    AdverseFlowAsymmetryAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = AdverseFlowAsymmetryAlpha().manifest
    assert m.alpha_id == "adverse_flow_asymmetry"
    assert m.data_fields == ("bid_qty", "ask_qty")
    assert m.paper_refs == ("129", "133")
    from research.registry.schemas import AlphaStatus

    assert m.status == AlphaStatus.DRAFT


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert AdverseFlowAsymmetryAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert AdverseFlowAsymmetryAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert AdverseFlowAsymmetryAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = AdverseFlowAsymmetryAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is AdverseFlowAsymmetryAlpha


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = AdverseFlowAsymmetryAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = AdverseFlowAsymmetryAlpha()
    assert alpha.get_signal() == 0.0


def test_single_update_returns_float() -> None:
    alpha = AdverseFlowAsymmetryAlpha()
    sig = alpha.update(500.0, 100.0)
    assert isinstance(sig, float)


def test_positive_qi_dominance_positive_signal() -> None:
    """Feed mostly positive qi -> ema_pos > ema_neg -> positive asymmetry."""
    alpha = AdverseFlowAsymmetryAlpha()
    for _ in range(100):
        sig = alpha.update(800.0, 100.0)
    assert sig > 0.5


def test_negative_qi_dominance_negative_signal() -> None:
    """Feed mostly negative qi -> negative asymmetry."""
    alpha = AdverseFlowAsymmetryAlpha()
    for _ in range(100):
        sig = alpha.update(100.0, 800.0)
    assert sig < -0.5


def test_balanced_flow_near_zero() -> None:
    """Alternating positive/negative qi -> asymmetry near 0."""
    alpha = AdverseFlowAsymmetryAlpha()
    for i in range(200):
        if i % 2 == 0:
            alpha.update(600.0, 400.0)
        else:
            alpha.update(400.0, 600.0)
    assert abs(alpha.get_signal()) < 0.1


def test_burst_detection() -> None:
    """Feed several large positive qi then balanced -> positive signal decays."""
    alpha = AdverseFlowAsymmetryAlpha()
    # Moderate positive burst (not fully saturated)
    for _ in range(10):
        alpha.update(700.0, 300.0)
    burst_signal = alpha.get_signal()
    assert burst_signal > 0.3
    # Now feed balanced for long enough to decay EMA-16 and EMA-8
    for _ in range(500):
        alpha.update(500.0, 500.0)
    decayed_signal = alpha.get_signal()
    assert abs(decayed_signal) < abs(burst_signal)


def test_signal_clipped_at_bounds() -> None:
    """Signal stays within [-1, 1]."""
    alpha = AdverseFlowAsymmetryAlpha()
    # Extreme positive
    for _ in range(500):
        sig = alpha.update(10000.0, 0.0)
    assert -1.0 <= sig <= 1.0
    # Extreme negative
    alpha.reset()
    for _ in range(500):
        sig = alpha.update(0.0, 10000.0)
    assert -1.0 <= sig <= 1.0


def test_zero_quantities_no_crash() -> None:
    """bid=0, ask=0 doesn't crash."""
    alpha = AdverseFlowAsymmetryAlpha()
    sig = alpha.update(0.0, 0.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_equal_quantities_zero_qi() -> None:
    """bid=ask -> qi=0 -> no contribution to either side."""
    alpha = AdverseFlowAsymmetryAlpha()
    for _ in range(100):
        sig = alpha.update(500.0, 500.0)
    assert abs(sig) < 1e-6


def test_reset_clears_state() -> None:
    alpha = AdverseFlowAsymmetryAlpha()
    alpha.update(800.0, 100.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should equal fresh instance
    fresh = AdverseFlowAsymmetryAlpha()
    s1 = alpha.update(300.0, 300.0)
    s2 = fresh.update(300.0, 300.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_get_signal_matches_update() -> None:
    alpha = AdverseFlowAsymmetryAlpha()
    ret = alpha.update(500.0, 100.0)
    assert ret == alpha.get_signal()


def test_ema_convergence() -> None:
    """After 200+ constant ticks, signal stabilizes."""
    alpha = AdverseFlowAsymmetryAlpha()
    for _ in range(200):
        alpha.update(700.0, 300.0)
    sig_a = alpha.get_signal()
    for _ in range(50):
        alpha.update(700.0, 300.0)
    sig_b = alpha.get_signal()
    assert sig_a == pytest.approx(sig_b, abs=1e-6)


def test_keyword_args() -> None:
    alpha = AdverseFlowAsymmetryAlpha()
    sig = alpha.update(bid_qty=500.0, ask_qty=100.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_positional_args() -> None:
    alpha = AdverseFlowAsymmetryAlpha()
    sig = alpha.update(500.0, 100.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_wrong_positional_count_raises() -> None:
    """1 arg raises ValueError."""
    alpha = AdverseFlowAsymmetryAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_asymmetry_bounded() -> None:
    """Raw asymmetry always in [-1, 1] by construction."""
    import numpy as np

    alpha = AdverseFlowAsymmetryAlpha()
    rng = np.random.default_rng(42)
    for _ in range(500):
        bq = rng.uniform(0, 1000)
        aq = rng.uniform(0, 1000)
        sig = alpha.update(bq, aq)
        assert -1.0 <= sig <= 1.0
        assert math.isfinite(sig)

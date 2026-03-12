"""Gate B correctness tests for CumOfiRevertAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.cum_ofi_revert.impl import (
    ALPHA_CLASS,
    CumOfiRevertAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert CumOfiRevertAlpha().manifest.alpha_id == "cum_ofi_revert"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert CumOfiRevertAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = CumOfiRevertAlpha().manifest.data_fields
    assert "ofi_l1_cum" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert CumOfiRevertAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert CumOfiRevertAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = CumOfiRevertAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is CumOfiRevertAlpha


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Before any update, signal should be zero."""
    alpha = CumOfiRevertAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_large_positive_cum_negative_signal() -> None:
    """Large positive cumOFI should produce a negative signal (reversion)."""
    alpha = CumOfiRevertAlpha()
    for _ in range(50):
        alpha.update(1000.0)
    assert alpha.get_signal() < 0.0


def test_large_negative_cum_positive_signal() -> None:
    """Large negative cumOFI should produce a positive signal (reversion)."""
    alpha = CumOfiRevertAlpha()
    for _ in range(50):
        alpha.update(-1000.0)
    assert alpha.get_signal() > 0.0


def test_zero_cum_zero_signal() -> None:
    """Zero cumOFI should yield zero signal."""
    alpha = CumOfiRevertAlpha()
    sig = alpha.update(0.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_convergence_constant_positive() -> None:
    """With constant positive input the signal converges to -1."""
    alpha = CumOfiRevertAlpha()
    for _ in range(500):
        alpha.update(500.0)
    # normalized → 500/500 = 1.0, EMA16 → 1.0, signal → -1.0
    assert alpha.get_signal() == pytest.approx(-1.0, abs=0.05)


def test_convergence_constant_negative() -> None:
    """With constant negative input the signal converges to +1."""
    alpha = CumOfiRevertAlpha()
    for _ in range(500):
        alpha.update(-500.0)
    assert alpha.get_signal() == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = CumOfiRevertAlpha()
    alpha.update(800.0)
    alpha.reset()
    sig = alpha.update(0.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_reset_makes_two_instances_equivalent() -> None:
    a1 = CumOfiRevertAlpha()
    a2 = CumOfiRevertAlpha()
    a1.update(999.0)
    a1.reset()
    s1 = a1.update(50.0)
    s2 = a2.update(50.0)
    assert s1 == pytest.approx(s2, abs=1e-12)


# ---------------------------------------------------------------------------
# get_signal
# ---------------------------------------------------------------------------


def test_get_signal_matches_update_return() -> None:
    alpha = CumOfiRevertAlpha()
    ret = alpha.update(123.0)
    assert alpha.get_signal() == ret


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_kwargs() -> None:
    alpha = CumOfiRevertAlpha()
    sig = alpha.update(ofi_l1_cum=200.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_update_accepts_positional() -> None:
    alpha = CumOfiRevertAlpha()
    sig = alpha.update(200.0)
    assert isinstance(sig, float)
    assert sig != 0.0


# ---------------------------------------------------------------------------
# Symmetry and boundedness
# ---------------------------------------------------------------------------


def test_symmetric_response() -> None:
    """Positive and negative inputs of equal magnitude yield opposite signals."""
    a_pos = CumOfiRevertAlpha()
    a_neg = CumOfiRevertAlpha()
    for _ in range(30):
        a_pos.update(500.0)
        a_neg.update(-500.0)
    assert a_pos.get_signal() == pytest.approx(-a_neg.get_signal(), abs=1e-9)


def test_signal_bounded() -> None:
    """Signal must stay in a reasonable bounded range under random input."""
    alpha = CumOfiRevertAlpha()
    rng = np.random.default_rng(42)
    vals = rng.uniform(-10000, 10000, 300)
    for v in vals:
        sig = alpha.update(float(v))
        # normalized is cum / ema64(|cum|) which can exceed 1 transiently,
        # but EMA16 smoothing keeps signal bounded in practice
        assert -10.0 <= sig <= 10.0


# ---------------------------------------------------------------------------
# Gradual buildup
# ---------------------------------------------------------------------------


def test_gradual_buildup() -> None:
    """Signal magnitude increases monotonically with repeated same-sign input."""
    alpha = CumOfiRevertAlpha()
    prev_abs = 0.0
    for i in range(1, 20):
        alpha.update(float(i * 100))
        cur_abs = abs(alpha.get_signal())
        # After initial warmup, signal should grow
        if i > 3:
            assert cur_abs >= prev_abs - 0.01  # allow small float tolerance
        prev_abs = cur_abs


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = CumOfiRevertAlpha()
    assert isinstance(alpha, AlphaProtocol)

"""Gate B correctness tests for OfiVolumeRatioAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.ofi_volume_ratio.impl import (
    ALPHA_CLASS,
    OfiVolumeRatioAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert OfiVolumeRatioAlpha().manifest.alpha_id == "ofi_volume_ratio"


def test_manifest_data_fields() -> None:
    fields = OfiVolumeRatioAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields
    assert "volume" in fields


def test_manifest_latency_profile_set() -> None:
    assert OfiVolumeRatioAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert OfiVolumeRatioAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = OfiVolumeRatioAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is OfiVolumeRatioAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues with volume -> OVR = 0."""
    alpha = OfiVolumeRatioAlpha()
    sig = alpha.update(100.0, 100.0, 500.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_dominant_signal_positive() -> None:
    """Bid-heavy flow -> positive signal."""
    alpha = OfiVolumeRatioAlpha()
    for _ in range(20):
        alpha.update(200.0, 0.0, 200.0)
    assert alpha.get_signal() > 0.9


def test_ask_dominant_signal_negative() -> None:
    """Ask-heavy flow -> negative signal."""
    alpha = OfiVolumeRatioAlpha()
    for _ in range(20):
        alpha.update(0.0, 200.0, 200.0)
    assert alpha.get_signal() < -0.9


def test_signal_scales_with_volume() -> None:
    """Higher volume reduces the raw ratio magnitude."""
    alpha1 = OfiVolumeRatioAlpha()
    alpha2 = OfiVolumeRatioAlpha()
    sig_low_vol = alpha1.update(100.0, 0.0, 100.0)   # raw = 1.0
    sig_high_vol = alpha2.update(100.0, 0.0, 1000.0)  # raw = 0.1
    assert abs(sig_low_vol) > abs(sig_high_vol)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_raw_ovr_constant_input() -> None:
    """EMA should converge to the raw OVR given constant input."""
    alpha = OfiVolumeRatioAlpha()
    bid, ask, vol = 300.0, 100.0, 400.0
    expected_ovr = (bid - ask) / vol
    for _ in range(100):
        alpha.update(bid, ask, vol)
    assert alpha.get_signal() == pytest.approx(expected_ovr, abs=1e-4)


def test_ema_single_step_initializes_to_raw_ovr() -> None:
    """First update initializes EMA to the raw OVR."""
    alpha = OfiVolumeRatioAlpha()
    bid, ask, vol = 400.0, 100.0, 500.0
    expected = (bid - ask) / vol
    sig = alpha.update(bid, ask, vol)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = OfiVolumeRatioAlpha()
    b1, a1, v1 = 300.0, 100.0, 400.0
    b2, a2, v2 = 100.0, 300.0, 400.0
    ovr1 = (b1 - a1) / v1
    ovr2 = (b2 - a2) / v2
    expected_ema2 = ovr1 + _EMA_ALPHA * (ovr2 - ovr1)
    alpha.update(b1, a1, v1)
    sig2 = alpha.update(b2, a2, v2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-9)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = OfiVolumeRatioAlpha()
    alpha.update(800.0, 100.0, 500.0)
    alpha.reset()
    sig = alpha.update(300.0, 300.0, 600.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = OfiVolumeRatioAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0, volume=300.0)
    expected = (200.0 - 100.0) / 300.0
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_wrong_positional_count_raises() -> None:
    alpha = OfiVolumeRatioAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_get_signal_before_update_is_zero() -> None:
    alpha = OfiVolumeRatioAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = OfiVolumeRatioAlpha()
    assert isinstance(alpha, AlphaProtocol)

"""Gate B correctness tests for SignedVolumeEmaAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.signed_volume_ema.impl import (
    ALPHA_CLASS,
    SignedVolumeEmaAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert SignedVolumeEmaAlpha().manifest.alpha_id == "signed_volume_ema"


def test_manifest_data_fields() -> None:
    fields = SignedVolumeEmaAlpha().manifest.data_fields
    assert "volume" in fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert SignedVolumeEmaAlpha().manifest.latency_profile is not None
    assert SignedVolumeEmaAlpha().manifest.latency_profile == "shioaji_sim_p95_v2026-03-04"


def test_manifest_feature_set_version() -> None:
    assert SignedVolumeEmaAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = SignedVolumeEmaAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is SignedVolumeEmaAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> signed volume = 0, signal = 0."""
    alpha = SignedVolumeEmaAlpha()
    sig = alpha.update(100.0, 100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_dominant_signal_positive() -> None:
    """Bid queue only -> imbalance = 1 -> signal positive."""
    alpha = SignedVolumeEmaAlpha()
    for _ in range(20):
        alpha.update(50.0, 200.0, 0.0)
    assert alpha.get_signal() > 0.0


def test_ask_dominant_signal_negative() -> None:
    """Ask queue only -> imbalance = -1 -> signal negative."""
    alpha = SignedVolumeEmaAlpha()
    for _ in range(20):
        alpha.update(50.0, 0.0, 200.0)
    assert alpha.get_signal() < 0.0


def test_zero_volume_signal_zero() -> None:
    """Zero volume -> signed volume = 0 regardless of imbalance."""
    alpha = SignedVolumeEmaAlpha()
    sig = alpha.update(0.0, 500.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_signal_scales_with_volume() -> None:
    """Higher volume should produce larger signal magnitude."""
    a1 = SignedVolumeEmaAlpha()
    a2 = SignedVolumeEmaAlpha()
    sig_small = a1.update(10.0, 300.0, 100.0)
    sig_large = a2.update(1000.0, 300.0, 100.0)
    assert abs(sig_large) > abs(sig_small)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_raw_sv_constant_input() -> None:
    """EMA should converge to the raw signed volume given constant input."""
    alpha = SignedVolumeEmaAlpha()
    vol, bid, ask = 100.0, 300.0, 100.0
    expected_sv = vol * (bid - ask) / (bid + ask)
    for _ in range(200):
        alpha.update(vol, bid, ask)
    assert alpha.get_signal() == pytest.approx(expected_sv, abs=1e-3)


def test_ema_single_step_initializes_to_raw_sv() -> None:
    """First update initializes EMA to the raw signed volume."""
    alpha = SignedVolumeEmaAlpha()
    vol, bid, ask = 50.0, 400.0, 100.0
    expected = vol * (bid - ask) / (bid + ask)
    sig = alpha.update(vol, bid, ask)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = SignedVolumeEmaAlpha()
    v1, b1, a1 = 100.0, 300.0, 100.0
    v2, b2, a2 = 50.0, 100.0, 300.0
    eps = 1e-8
    sv1 = v1 * (b1 - a1) / (b1 + a1 + eps)
    sv2 = v2 * (b2 - a2) / (b2 + a2 + eps)
    expected_ema2 = sv1 + _EMA_ALPHA * (sv2 - sv1)
    alpha.update(v1, b1, a1)
    sig2 = alpha.update(v2, b2, a2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-9)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = SignedVolumeEmaAlpha()
    alpha.update(100.0, 800.0, 100.0)
    alpha.reset()
    sig = alpha.update(50.0, 300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility -- kwargs
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = SignedVolumeEmaAlpha()
    sig = alpha.update(volume=80.0, bid_qty=200.0, ask_qty=100.0)
    expected = 80.0 * (200.0 - 100.0) / (200.0 + 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_one_arg_raises() -> None:
    alpha = SignedVolumeEmaAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_update_two_args_raises() -> None:
    alpha = SignedVolumeEmaAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)


def test_get_signal_before_update_is_zero() -> None:
    alpha = SignedVolumeEmaAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = SignedVolumeEmaAlpha()
    assert isinstance(alpha, AlphaProtocol)

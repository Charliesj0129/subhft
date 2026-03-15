"""Gate B correctness tests for VolumeSurpriseAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.volume_surprise.impl import (
    ALPHA_CLASS,
    VolumeSurpriseAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert VolumeSurpriseAlpha().manifest.alpha_id == "volume_surprise"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert VolumeSurpriseAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = VolumeSurpriseAlpha().manifest.data_fields
    assert "volume" in fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert VolumeSurpriseAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert VolumeSurpriseAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = VolumeSurpriseAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is VolumeSurpriseAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First update seeds EMA, signal should be 0."""
    alpha = VolumeSurpriseAlpha()
    sig = alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_high_volume_bid_dominant_positive() -> None:
    """High volume surprise + bid dominant -> positive signal."""
    alpha = VolumeSurpriseAlpha()
    # Seed with normal volume
    for _ in range(50):
        alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    # Spike volume with bid dominant
    sig = alpha.update(volume=500.0, bid_qty=300.0, ask_qty=100.0)
    assert sig > 0.0


def test_high_volume_ask_dominant_negative() -> None:
    """High volume surprise + ask dominant -> negative signal."""
    alpha = VolumeSurpriseAlpha()
    for _ in range(50):
        alpha.update(volume=100.0, bid_qty=100.0, ask_qty=200.0)
    sig = alpha.update(volume=500.0, bid_qty=100.0, ask_qty=300.0)
    assert sig < 0.0


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> direction = 0, signal = 0."""
    alpha = VolumeSurpriseAlpha()
    alpha.update(volume=100.0, bid_qty=100.0, ask_qty=100.0)
    sig = alpha.update(volume=200.0, bid_qty=100.0, ask_qty=100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_normal_volume_small_signal() -> None:
    """When volume matches EMA, surprise ~ 0, signal ~ 0."""
    alpha = VolumeSurpriseAlpha()
    for _ in range(100):
        alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    sig = alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    assert abs(sig) < 0.05


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_constant_volume() -> None:
    """EMA should converge to constant volume after enough ticks."""
    alpha = VolumeSurpriseAlpha()
    for _ in range(200):
        alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    # At convergence, surprise = 100/100 - 1 = 0
    sig = alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    assert abs(sig) < 1e-6


def test_ema_manual_second_step() -> None:
    """Verify EMA computation on second step."""
    alpha = VolumeSurpriseAlpha()
    v1, v2 = 100.0, 200.0
    alpha.update(volume=v1, bid_qty=200.0, ask_qty=100.0)  # seeds EMA=v1
    # After first tick, EMA = v1 = 100
    # surprise = v2 / v1 - 1 = 1.0
    # direction = +1 (bid > ask)
    # signal = 1.0
    sig = alpha.update(volume=v2, bid_qty=200.0, ask_qty=100.0)
    expected_surprise = v2 / v1 - 1.0
    assert sig == pytest.approx(expected_surprise, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = VolumeSurpriseAlpha()
    sig = alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_positional_args() -> None:
    alpha = VolumeSurpriseAlpha()
    sig = alpha.update(100.0, 200.0, 100.0)  # volume, bid_qty, ask_qty
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = VolumeSurpriseAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_update_two_args_raises() -> None:
    alpha = VolumeSurpriseAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)


def test_reset_clears_state() -> None:
    alpha = VolumeSurpriseAlpha()
    alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    alpha.update(volume=500.0, bid_qty=300.0, ask_qty=100.0)
    alpha.reset()
    # After reset, first update should return 0 (re-seeds EMA)
    sig = alpha.update(volume=100.0, bid_qty=200.0, ask_qty=100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = VolumeSurpriseAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = VolumeSurpriseAlpha()
    assert isinstance(alpha, AlphaProtocol)

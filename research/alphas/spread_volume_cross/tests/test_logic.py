"""Gate B correctness tests for SpreadVolumeCrossAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.spread_volume_cross.impl import (
    ALPHA_CLASS,
    SpreadVolumeCrossAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert SpreadVolumeCrossAlpha().manifest.alpha_id == "spread_volume_cross"


def test_manifest_data_fields() -> None:
    fields = SpreadVolumeCrossAlpha().manifest.data_fields
    assert "spread_bps" in fields
    assert "volume" in fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert SpreadVolumeCrossAlpha().manifest.latency_profile == "shioaji_sim_p95_v2026-03-04"


def test_manifest_feature_set_version() -> None:
    assert SpreadVolumeCrossAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = SpreadVolumeCrossAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is SpreadVolumeCrossAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First tick initializes baselines; signal should be 0."""
    alpha = SpreadVolumeCrossAlpha()
    sig = alpha.update(spread_bps=10.0, volume=100.0, bid_qty=50.0, ask_qty=50.0)
    assert sig == 0.0


def test_no_spread_change_zero_signal() -> None:
    """No spread change => delta_spread=0 => raw=0 => signal stays near 0."""
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=10.0, volume=100.0, bid_qty=50.0, ask_qty=50.0)
    sig = alpha.update(spread_bps=10.0, volume=100.0, bid_qty=100.0, ask_qty=50.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_spread_narrowing_with_volume_spike_and_bid_dominant() -> None:
    """Spread narrows + volume spikes + bid dominant => positive signal."""
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=20.0, volume=100.0, bid_qty=100.0, ask_qty=50.0)
    # Spread narrows (20->10), volume spikes, bid dominant
    sig = alpha.update(spread_bps=10.0, volume=500.0, bid_qty=200.0, ask_qty=50.0)
    assert sig > 0.0


def test_spread_narrowing_with_volume_spike_and_ask_dominant() -> None:
    """Spread narrows + volume spikes + ask dominant => negative signal."""
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=20.0, volume=100.0, bid_qty=50.0, ask_qty=100.0)
    # Spread narrows (20->10), volume spikes, ask dominant
    sig = alpha.update(spread_bps=10.0, volume=500.0, bid_qty=50.0, ask_qty=200.0)
    assert sig < 0.0


def test_spread_widening_with_bid_dominant_gives_negative() -> None:
    """Spread widens (positive delta) + bid dominant => raw is negative (selling)."""
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=10.0, volume=100.0, bid_qty=200.0, ask_qty=50.0)
    # Spread widens (10->30), bid dominant
    sig = alpha.update(spread_bps=30.0, volume=200.0, bid_qty=200.0, ask_qty=50.0)
    assert sig < 0.0


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_converges_constant_input() -> None:
    """Under constant inputs, signal should converge to the raw cross value."""
    alpha = SpreadVolumeCrossAlpha()
    # First tick: initializes
    alpha.update(spread_bps=20.0, volume=100.0, bid_qty=100.0, ask_qty=50.0)
    # Feed many ticks with same values (delta_spread=0 => raw=0)
    for _ in range(200):
        alpha.update(spread_bps=20.0, volume=100.0, bid_qty=100.0, ask_qty=50.0)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-4)


def test_ema_convergence_persistent_narrowing() -> None:
    """Persistent spread narrowing with bid dominance => signal stays positive."""
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=50.0, volume=100.0, bid_qty=200.0, ask_qty=100.0)
    # Each tick narrows the spread by 0.5 bps
    spread = 50.0
    for _ in range(100):
        spread -= 0.1
        alpha.update(spread_bps=spread, volume=150.0, bid_qty=200.0, ask_qty=100.0)
    assert alpha.get_signal() > 0.0


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = SpreadVolumeCrossAlpha()
    alpha.update(spread_bps=20.0, volume=100.0, bid_qty=200.0, ask_qty=50.0)
    alpha.update(spread_bps=10.0, volume=500.0, bid_qty=200.0, ask_qty=50.0)
    alpha.reset()
    # After reset, first update should return 0
    sig = alpha.update(spread_bps=15.0, volume=100.0, bid_qty=100.0, ask_qty=100.0)
    assert sig == 0.0


# ---------------------------------------------------------------------------
# Kwargs API
# ---------------------------------------------------------------------------


def test_update_accepts_kwargs() -> None:
    alpha = SpreadVolumeCrossAlpha()
    sig = alpha.update(spread_bps=10.0, volume=100.0, bid_qty=50.0, ask_qty=50.0)
    assert isinstance(sig, float)


def test_get_signal_before_update_is_zero() -> None:
    alpha = SpreadVolumeCrossAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = SpreadVolumeCrossAlpha()
    assert isinstance(alpha, AlphaProtocol)

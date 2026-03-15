"""Gate B correctness tests for QuoteIntensityAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.quote_intensity.impl import (
    ALPHA_CLASS,
    QuoteIntensityAlpha,
    _EMA_ALPHA_FAST,
    _EMA_ALPHA_SLOW,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert QuoteIntensityAlpha().manifest.alpha_id == "quote_intensity"


def test_manifest_data_fields() -> None:
    fields = QuoteIntensityAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert QuoteIntensityAlpha().manifest.latency_profile is not None
    assert "p95" in QuoteIntensityAlpha().manifest.latency_profile


def test_manifest_feature_set_version() -> None:
    assert QuoteIntensityAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = QuoteIntensityAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS
    assert len(m.roles_used) > 0
    assert len(m.skills_used) > 0


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is QuoteIntensityAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues => direction = 0, signal = 0."""
    alpha = QuoteIntensityAlpha()
    # First call: deltas from (0,0) to (100,100) — equal deltas, direction=0.
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_dominant_signal_positive() -> None:
    """When bid_qty > ask_qty, signal should be positive."""
    alpha = QuoteIntensityAlpha()
    # Warmup with zero to establish baseline.
    alpha.update(0.0, 0.0)
    sig = alpha.update(200.0, 50.0)
    assert sig > 0.0


def test_ask_dominant_signal_negative() -> None:
    """When ask_qty > bid_qty, signal should be negative."""
    alpha = QuoteIntensityAlpha()
    alpha.update(0.0, 0.0)
    sig = alpha.update(50.0, 200.0)
    assert sig < 0.0


def test_no_change_signal_decays() -> None:
    """When quantities stop changing, activity EMAs decay toward zero, signal decays."""
    alpha = QuoteIntensityAlpha()
    alpha.update(0.0, 0.0)
    alpha.update(100.0, 50.0)  # spike in activity
    sig_initial = alpha.get_signal()
    # Feed constant values — deltas become 0.
    for _ in range(50):
        alpha.update(100.0, 50.0)
    sig_decayed = alpha.get_signal()
    # Signal magnitude should decrease as activity dies out.
    # With zero deltas, EMA_fast decays faster than EMA_slow,
    # so ratio decreases toward 0.
    assert abs(sig_decayed) < abs(sig_initial)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_first_step_initializes() -> None:
    """First update initializes both EMAs to the raw activity."""
    alpha = QuoteIntensityAlpha()
    # First call: deltas from (0,0) to (100,200) => activity = 300.
    sig = alpha.update(100.0, 200.0)
    # direction = -1 (ask > bid), ratio = EMA_fast/EMA_slow = 1.0
    assert sig == pytest.approx(-1.0, abs=1e-6)


def test_ema_convergence_constant_activity() -> None:
    """With constant deltas, EMA_fast/EMA_slow converges to 1.0."""
    alpha = QuoteIntensityAlpha()
    # Alternate between two states to create constant deltas.
    for i in range(200):
        if i % 2 == 0:
            alpha.update(200.0, 100.0)
        else:
            alpha.update(100.0, 200.0)
    # After many steps with symmetric alternation, both EMAs track the same
    # constant activity level, so ratio -> 1.0.
    # Direction alternates, so we just check the last signal magnitude.
    assert abs(alpha.get_signal()) == pytest.approx(1.0, abs=0.05)


def test_ema_second_step_manual() -> None:
    """Second step: verify EMA update formula manually."""
    alpha = QuoteIntensityAlpha()
    # Step 1: (0,0) -> (100,200). Activity = 300.
    alpha.update(100.0, 200.0)
    # Step 2: (100,200) -> (150,100). Deltas: |50| + |100| = 150.
    sig = alpha.update(150.0, 100.0)
    # EMA_fast = 300 + alpha_fast*(150-300)
    expected_fast = 300.0 + _EMA_ALPHA_FAST * (150.0 - 300.0)
    expected_slow = 300.0 + _EMA_ALPHA_SLOW * (150.0 - 300.0)
    expected_ratio = expected_fast / expected_slow
    # direction = +1 (bid > ask: 150 > 100)
    assert sig == pytest.approx(expected_ratio, abs=1e-6)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = QuoteIntensityAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)
    assert sig > 0.0  # bid > ask => positive


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = QuoteIntensityAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = QuoteIntensityAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = QuoteIntensityAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(200.0, 500.0)
    alpha.reset()
    # After reset, should behave identically to a fresh instance.
    fresh = QuoteIntensityAlpha()
    s1 = alpha.update(300.0, 300.0)
    s2 = fresh.update(300.0, 300.0)
    assert s1 == s2


def test_get_signal_before_update_is_zero() -> None:
    alpha = QuoteIntensityAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = QuoteIntensityAlpha()
    assert isinstance(alpha, AlphaProtocol)

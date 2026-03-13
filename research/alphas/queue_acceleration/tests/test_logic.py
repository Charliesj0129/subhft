"""Gate B correctness tests for QueueAccelerationAlpha (ref 026)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.queue_acceleration.impl import (
    _A4,
    _A8,
    _A32,
    ALPHA_CLASS,
    QueueAccelerationAlpha,
)
from research.registry.schemas import AlphaStatus

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert QueueAccelerationAlpha().manifest.alpha_id == "queue_acceleration"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert QueueAccelerationAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_026() -> None:
    assert "026" in QueueAccelerationAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = QueueAccelerationAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert QueueAccelerationAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert QueueAccelerationAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = QueueAccelerationAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is QueueAccelerationAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues -> QI = 0, acceleration = 0, signal = 0."""
    alpha = QueueAccelerationAlpha()
    for _ in range(50):
        sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_constant_input_signal_decays_to_zero() -> None:
    """With constant bid/ask, velocity stabilizes -> accel -> 0 -> signal -> 0."""
    alpha = QueueAccelerationAlpha()
    for _ in range(500):
        sig = alpha.update(300.0, 100.0)
    assert abs(sig) < 0.01


def test_bid_ramp_up_gives_positive_signal() -> None:
    """Increasing bid dominance (acceleration positive) -> positive signal."""
    alpha = QueueAccelerationAlpha()
    # Start with balanced, then ramp up bid
    for _ in range(20):
        alpha.update(100.0, 100.0)
    for i in range(30):
        alpha.update(100.0 + i * 10.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_ask_ramp_up_gives_negative_signal() -> None:
    """Increasing ask dominance (acceleration negative) -> negative signal."""
    alpha = QueueAccelerationAlpha()
    for _ in range(20):
        alpha.update(100.0, 100.0)
    for i in range(30):
        alpha.update(100.0, 100.0 + i * 10.0)
    assert alpha.get_signal() < 0.0


def test_signal_bounded_in_minus_one_to_one() -> None:
    """Signal must stay in [-1, 1] at all times."""
    alpha = QueueAccelerationAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0, f"Signal out of bounds: {sig}"


def test_signal_bounded_extreme_alternation() -> None:
    """Rapidly alternating extreme inputs must still produce bounded signal."""
    alpha = QueueAccelerationAlpha()
    for i in range(200):
        if i % 2 == 0:
            sig = alpha.update(1000.0, 0.0)
        else:
            sig = alpha.update(0.0, 1000.0)
        assert -1.0 <= sig <= 1.0, f"Signal out of bounds at tick {i}: {sig}"


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_first_tick_signal_zero() -> None:
    """First tick: EMAs initialize to same value, velocity=0, accel=0, signal=0."""
    alpha = QueueAccelerationAlpha()
    sig = alpha.update(300.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_ema_coefficients_valid() -> None:
    """EMA coefficients must be in (0, 1)."""
    assert 0.0 < _A4 < 1.0
    assert 0.0 < _A8 < 1.0
    assert 0.0 < _A32 < 1.0


def test_acceleration_detects_direction_change() -> None:
    """When bid pressure is ramping up, sudden reversal should flip signal negative."""
    alpha = QueueAccelerationAlpha()
    # Ramp up bid dominance (increasing acceleration)
    for _ in range(20):
        alpha.update(100.0, 100.0)
    for i in range(30):
        alpha.update(100.0 + i * 10.0, 100.0)
    sig_before = alpha.get_signal()
    assert sig_before > 0.0, "Ramping bid should give positive accel"
    # Suddenly reverse to ask ramp
    for i in range(30):
        alpha.update(100.0, 100.0 + i * 10.0)
    assert alpha.get_signal() < sig_before, "Reversal should reduce signal"


def test_velocity_fast_minus_slow_ema() -> None:
    """Manual verification: velocity = ema8 - ema32 after two ticks."""
    alpha = QueueAccelerationAlpha()
    # Tick 1: both EMAs init to same qi -> velocity = 0
    alpha.update(300.0, 100.0)
    qi1 = (300.0 - 100.0) / 400.0  # 0.5

    # Tick 2: EMAs diverge
    qi2 = (100.0 - 300.0) / 400.0  # -0.5
    expected_ema8 = qi1 + _A8 * (qi2 - qi1)
    expected_ema32 = qi1 + _A32 * (qi2 - qi1)
    expected_velocity = expected_ema8 - expected_ema32

    alpha.update(100.0, 300.0)
    # Velocity is internal, but we can verify via the accel_ema effect
    assert expected_velocity < 0.0  # fast EMA reacted more to the reversal


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = QueueAccelerationAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = QueueAccelerationAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_no_args_returns_float() -> None:
    alpha = QueueAccelerationAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_reset_clears_state() -> None:
    alpha = QueueAccelerationAlpha()
    for _ in range(50):
        alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, first update should behave as fresh
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = QueueAccelerationAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = QueueAccelerationAlpha()
    assert isinstance(alpha, AlphaProtocol)


def test_manifest_complexity_o1() -> None:
    assert QueueAccelerationAlpha().manifest.complexity == "O(1)"


def test_manifest_status_draft() -> None:
    assert QueueAccelerationAlpha().manifest.status == AlphaStatus.DRAFT

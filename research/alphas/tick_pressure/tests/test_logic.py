"""Gate B correctness tests for TickPressureAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.tick_pressure.impl import (
    ALPHA_CLASS,
    TickPressureAlpha,
    _EMA_ALPHA_8,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert TickPressureAlpha().manifest.alpha_id == "tick_pressure"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert TickPressureAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = TickPressureAlpha().manifest.data_fields
    assert "mid_price_x2" in fields
    assert "l1_bid_qty" in fields
    assert "l1_ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert TickPressureAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert TickPressureAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = TickPressureAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is TickPressureAlpha


# ---------------------------------------------------------------------------
# First tick / initialization
# ---------------------------------------------------------------------------


def test_initial_zero() -> None:
    """First tick always returns 0 (no previous mid to compare)."""
    alpha = TickPressureAlpha()
    sig = alpha.update(100000, 50.0, 50.0)
    assert sig == 0.0


def test_first_zero_kwargs() -> None:
    """First tick via kwargs also returns 0."""
    alpha = TickPressureAlpha()
    sig = alpha.update(mid_price_x2=100000, l1_bid_qty=50.0, l1_ask_qty=50.0)
    assert sig == 0.0


def test_second_nonzero() -> None:
    """Second tick with a mid-price change produces a non-zero signal."""
    alpha = TickPressureAlpha()
    alpha.update(100000, 50.0, 50.0)
    sig = alpha.update(100010, 50.0, 50.0)
    assert sig != 0.0


# ---------------------------------------------------------------------------
# Directional correctness
# ---------------------------------------------------------------------------


def test_uptick_large_queue_positive() -> None:
    """Upward tick with large queue → positive signal."""
    alpha = TickPressureAlpha()
    alpha.update(100000, 500.0, 500.0)
    sig = alpha.update(100020, 500.0, 500.0)
    assert sig > 0.0


def test_downtick_large_queue_negative() -> None:
    """Downward tick with large queue → negative signal."""
    alpha = TickPressureAlpha()
    alpha.update(100000, 500.0, 500.0)
    sig = alpha.update(99980, 500.0, 500.0)
    assert sig < 0.0


def test_no_tick_zero_contribution() -> None:
    """No mid-price change → sign = 0, raw_pressure = 0, EMA decays toward 0."""
    alpha = TickPressureAlpha()
    alpha.update(100000, 100.0, 100.0)
    sig = alpha.update(100000, 100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_symmetric() -> None:
    """Symmetric up/down ticks with equal queues should give opposite signals."""
    a_up = TickPressureAlpha()
    a_up.update(100000, 200.0, 200.0)
    sig_up = a_up.update(100010, 200.0, 200.0)

    a_dn = TickPressureAlpha()
    a_dn.update(100000, 200.0, 200.0)
    sig_dn = a_dn.update(99990, 200.0, 200.0)

    assert sig_up == pytest.approx(-sig_dn, abs=1e-9)


# ---------------------------------------------------------------------------
# Queue size effects
# ---------------------------------------------------------------------------


def test_small_queue_weak() -> None:
    """Small queue relative to baseline produces a weaker signal."""
    alpha = TickPressureAlpha()
    # Build up baseline with large queue
    for _ in range(100):
        alpha.update(100000, 1000.0, 1000.0)
    # Now uptick with small queue
    sig = alpha.update(100010, 10.0, 10.0)
    assert abs(sig) < 0.1  # weak signal


def test_large_queue_strong() -> None:
    """Queue much larger than baseline → ratio > 1 → amplified signal."""
    alpha = TickPressureAlpha()
    # Build baseline with small queue
    for _ in range(100):
        alpha.update(100000, 10.0, 10.0)
    # Uptick with large queue
    sig = alpha.update(100010, 1000.0, 1000.0)
    assert sig > 0.0  # positive direction
    # Should be amplified since queue >> baseline
    assert abs(sig) > _EMA_ALPHA_8 * 0.5


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_convergence() -> None:
    """Repeated identical upticks converge the signal."""
    alpha = TickPressureAlpha()
    mid = 100000
    signals = []
    for i in range(200):
        mid += 10  # constant uptick
        sig = alpha.update(mid, 100.0, 100.0)
        signals.append(sig)
    # Signal should stabilize (last 10 values very similar)
    tail = signals[-10:]
    assert max(tail) - min(tail) < 0.01


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_kwargs() -> None:
    alpha = TickPressureAlpha()
    sig = alpha.update(mid_price_x2=100000, l1_bid_qty=100.0, l1_ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_accepts_positional() -> None:
    alpha = TickPressureAlpha()
    sig = alpha.update(100000, 100.0, 100.0)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = TickPressureAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_update_two_args_raises() -> None:
    alpha = TickPressureAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 200.0)


def test_reset_clears_state() -> None:
    alpha = TickPressureAlpha()
    alpha.update(100000, 500.0, 500.0)
    alpha.update(100020, 500.0, 500.0)
    alpha.reset()
    # After reset, first update should return 0 (no history)
    sig = alpha.update(100000, 300.0, 300.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = TickPressureAlpha()
    assert alpha.get_signal() == 0.0


def test_get_signal_matches_update_return() -> None:
    alpha = TickPressureAlpha()
    alpha.update(100000, 100.0, 100.0)
    ret = alpha.update(100010, 100.0, 100.0)
    assert alpha.get_signal() == ret


def test_bounded() -> None:
    """Signal should stay bounded under random input."""
    alpha = TickPressureAlpha()
    rng = np.random.default_rng(42)
    mids = np.cumsum(rng.choice([-10, 0, 10], size=500)) + 100000
    bids = rng.uniform(1, 1000, 500)
    asks = rng.uniform(1, 1000, 500)
    for m, b, a in zip(mids, bids, asks):
        sig = alpha.update(float(m), float(b), float(a))
        # Signal is EMA of values that can exceed 1 (queue_ratio > 1),
        # but should not blow up; check a generous bound.
        assert abs(sig) < 100.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = TickPressureAlpha()
    assert isinstance(alpha, AlphaProtocol)

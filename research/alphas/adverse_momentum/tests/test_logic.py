"""Gate B correctness tests for AdverseMomentumAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.adverse_momentum.impl import (
    ALPHA_CLASS,
    AdverseMomentumAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert AdverseMomentumAlpha().manifest.alpha_id == "adverse_momentum"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert AdverseMomentumAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = AdverseMomentumAlpha().manifest.data_fields
    assert "mid_price" in fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert AdverseMomentumAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert AdverseMomentumAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = AdverseMomentumAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is AdverseMomentumAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_zero_queues_signal_zero() -> None:
    """When both queues are zero, imbalance is ~0 so signal stays ~0."""
    alpha = AdverseMomentumAlpha()
    for i in range(50):
        sig = alpha.update(100.0 + i * 0.1, 0.0, 0.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_no_price_change_signal_zero() -> None:
    """When mid_price is constant, delta_price=0 so raw=0 and signal stays 0."""
    alpha = AdverseMomentumAlpha()
    for _ in range(50):
        sig = alpha.update(100.0, 500.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-6)


def test_positive_imbalance_price_up_positive_signal() -> None:
    """Bid-heavy imbalance + price going up -> positive signal."""
    alpha = AdverseMomentumAlpha()
    mid = 100.0
    for _ in range(100):
        mid += 1.0
        alpha.update(mid, 500.0, 100.0)  # bid > ask, price up
    assert alpha.get_signal() > 0.0


def test_positive_imbalance_price_down_negative_signal() -> None:
    """Bid-heavy imbalance + price going down -> negative signal."""
    alpha = AdverseMomentumAlpha()
    mid = 1000.0
    for _ in range(100):
        mid -= 1.0
        alpha.update(mid, 500.0, 100.0)  # bid > ask, price down
    assert alpha.get_signal() < 0.0


def test_signal_bounded() -> None:
    """Random fuzz: signal must always be in [-2, 2]."""
    alpha = AdverseMomentumAlpha()
    rng = np.random.default_rng(42)
    mids = np.cumsum(rng.normal(0, 10, 500)) + 1000.0
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    for m, b, a in zip(mids, bids, asks):
        sig = alpha.update(float(m), float(b), float(a))
        assert -2.0 <= sig <= 2.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence_constant_inputs() -> None:
    """With constant delta_price and constant imbalance, EMA converges."""
    alpha = AdverseMomentumAlpha()
    mid = 100.0
    for _ in range(1000):
        mid += 1.0  # constant delta = 1.0
        alpha.update(mid, 300.0, 100.0)  # constant imbalance = 0.5
    # raw = 1.0 * 0.5 = 0.5; EMA should converge to 0.5
    assert alpha.get_signal() == pytest.approx(0.5, abs=0.01)


def test_first_update_returns_zero() -> None:
    """First update should return 0.0 (no previous mid to compute delta)."""
    alpha = AdverseMomentumAlpha()
    sig = alpha.update(100.0, 500.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)
    assert alpha._initialized is True


def test_second_update_ema_step() -> None:
    """Second update: EMA = 0 + alpha * (raw - 0) = alpha * raw."""
    alpha = AdverseMomentumAlpha()
    # Tick 1: mid=100, bid=300, ask=100 -> imbalance = 0.5
    alpha.update(100.0, 300.0, 100.0)
    # Tick 2: mid=110 -> delta=10, lagged_imbalance=0.5 -> raw=5.0
    sig = alpha.update(110.0, 200.0, 200.0)
    expected = _EMA_ALPHA * 5.0  # EMA from 0 toward 5.0
    assert sig == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = AdverseMomentumAlpha()
    sig = alpha.update(mid_price=100.0, bid_qty=500.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_wrong_arg_count_raises() -> None:
    alpha = AdverseMomentumAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 5.0)  # only 2 positional, need 3


def test_reset_clears_state() -> None:
    alpha = AdverseMomentumAlpha()
    alpha.update(100.0, 500.0, 100.0)
    alpha.update(110.0, 300.0, 200.0)
    alpha.reset()
    # After reset, first update should behave like fresh instance
    sig = alpha.update(100.0, 0.0, 0.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_zero() -> None:
    alpha = AdverseMomentumAlpha()
    assert alpha.get_signal() == 0.0


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = AdverseMomentumAlpha()
    assert isinstance(alpha, AlphaProtocol)

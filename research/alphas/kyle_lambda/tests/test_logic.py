"""Gate B correctness tests for KyleLambdaAlpha (ref 135)."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.kyle_lambda.impl import (
    ALPHA_CLASS,
    KyleLambdaAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert KyleLambdaAlpha().manifest.alpha_id == "kyle_lambda"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert KyleLambdaAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_135() -> None:
    assert "135" in KyleLambdaAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = KyleLambdaAlpha().manifest.data_fields
    assert "mid_price" in fields
    assert "volume" in fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert KyleLambdaAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert KyleLambdaAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = KyleLambdaAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is KyleLambdaAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_zero_volume_does_not_crash() -> None:
    """Zero volume should not raise; signal remains finite."""
    alpha = KyleLambdaAlpha()
    sig = alpha.update(100.0, 0.0, 50.0, 50.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_equal_queues_neutral() -> None:
    """bid_qty == ask_qty -> signed_vol = 0 -> lambda ~ 0."""
    alpha = KyleLambdaAlpha()
    for i in range(50):
        sig = alpha.update(100.0 + i * 0.01, 100.0, 50.0, 50.0)
    # With zero signed volume, covariance is zero, lambda is zero
    assert abs(sig) < 0.1


def test_positive_correlation_positive_lambda() -> None:
    """Positive correlation between dP and signed_vol yields positive lambda.

    We alternate between 'buy ticks' (rising price + buy-side imbalance) and
    'sell ticks' (falling price + sell-side imbalance) so that Cov(dP, signed_vol) > 0
    while Var(signed_vol) > 0 (non-degenerate).
    """
    alpha = KyleLambdaAlpha()
    for i in range(200):
        if i % 2 == 0:
            # Buy tick: price rises, bid_qty > ask_qty
            mid = 100.0 + (i // 2) * 0.2
            vol = 100.0
            bid_qty = 200.0
            ask_qty = 50.0
        else:
            # Sell tick: price falls, ask_qty > bid_qty
            mid = 100.0 + (i // 2) * 0.2 - 0.1
            vol = 100.0
            bid_qty = 50.0
            ask_qty = 200.0
        alpha.update(mid, vol, bid_qty, ask_qty)
    assert alpha.get_signal() > 0.0


def test_signal_bounded() -> None:
    """Random fuzz -> signal must stay in [-2, 2]."""
    alpha = KyleLambdaAlpha()
    rng = np.random.default_rng(42)
    mids = rng.uniform(90, 110, 500)
    vols = rng.uniform(0, 1000, 500)
    bids = rng.uniform(0, 500, 500)
    asks = rng.uniform(0, 500, 500)
    for m, v, b, a in zip(mids, vols, bids, asks):
        sig = alpha.update(m, v, b, a)
        assert -2.0 <= sig <= 2.0, f"Signal {sig} out of bounds"


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_convergence() -> None:
    """Constant inputs should converge to a stable signal."""
    alpha = KyleLambdaAlpha()
    signals = []
    for i in range(500):
        sig = alpha.update(100.0 + 0.01, 100.0, 200.0, 50.0)
        signals.append(sig)
    # Last 50 signals should be very stable
    last_50 = signals[-50:]
    assert max(last_50) - min(last_50) < 0.01


def test_first_update_initializes() -> None:
    """First update should set _initialized to True and return a float."""
    alpha = KyleLambdaAlpha()
    assert not alpha._initialized
    sig = alpha.update(100.0, 50.0, 200.0, 100.0)
    assert alpha._initialized
    assert isinstance(sig, float)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = KyleLambdaAlpha()
    sig = alpha.update(mid_price=100.0, volume=50.0, bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_wrong_arg_count_raises() -> None:
    """1, 2, or 3 positional args should raise ValueError."""
    alpha = KyleLambdaAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 50.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 50.0, 200.0)


def test_reset_clears_state() -> None:
    alpha = KyleLambdaAlpha()
    alpha.update(100.0, 50.0, 200.0, 100.0)
    alpha.update(101.0, 60.0, 210.0, 90.0)
    alpha.reset()
    assert not alpha._initialized
    assert alpha._signal == 0.0
    assert alpha._prev_mid == 0.0
    # After reset, first update should behave like fresh instance
    sig = alpha.update(100.0, 50.0, 200.0, 100.0)
    fresh = KyleLambdaAlpha()
    sig_fresh = fresh.update(100.0, 50.0, 200.0, 100.0)
    assert sig == sig_fresh


def test_get_signal_before_update_zero() -> None:
    alpha = KyleLambdaAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = KyleLambdaAlpha()
    assert isinstance(alpha, AlphaProtocol)

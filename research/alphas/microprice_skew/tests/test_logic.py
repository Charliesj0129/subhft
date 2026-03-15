"""Gate B correctness tests for MicropriceSkewAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.microprice_skew.impl import (
    _EMA_ALPHA_8,
    _EPSILON,
    ALPHA_CLASS,
    MicropriceSkewAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MicropriceSkewAlpha().manifest.alpha_id == "microprice_skew"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert MicropriceSkewAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_data_fields() -> None:
    fields = MicropriceSkewAlpha().manifest.data_fields
    assert fields == ("bid_px", "ask_px", "bid_qty", "ask_qty", "mid_price")


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert MicropriceSkewAlpha().manifest.latency_profile == "shioaji_sim_p95_v2026-03-04"


def test_manifest_feature_set_version() -> None:
    assert MicropriceSkewAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MicropriceSkewAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MicropriceSkewAlpha


# ---------------------------------------------------------------------------
# Signal boundary and basic behavior
# ---------------------------------------------------------------------------


def test_initial_zero() -> None:
    """get_signal() before any update is zero."""
    alpha = MicropriceSkewAlpha()
    assert alpha.get_signal() == 0.0


def test_equal_depths_zero_skew() -> None:
    """When bid_qty == ask_qty, microprice == mid_price, signal ~ 0."""
    alpha = MicropriceSkewAlpha()
    # With equal depths: microprice = (ask*bid_qty + bid*ask_qty) / (bid_qty+ask_qty)
    # = (ask + bid) * qty / (2*qty) = (ask+bid)/2 = mid_price
    sig = alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    assert abs(sig) < 1e-6


def test_convergence_equal_depths() -> None:
    """Sustained equal depths should converge signal to 0."""
    alpha = MicropriceSkewAlpha()
    for _ in range(200):
        alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    assert abs(alpha.get_signal()) < 1e-6


def test_bid_heavy_positive() -> None:
    """When bid_qty >> ask_qty, microprice > mid => positive signal."""
    alpha = MicropriceSkewAlpha()
    for _ in range(20):
        alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=100.0, ask_qty=10.0, mid_price=101.0)
    assert alpha.get_signal() > 0.0


def test_ask_heavy_negative() -> None:
    """When ask_qty >> bid_qty, microprice < mid => negative signal."""
    alpha = MicropriceSkewAlpha()
    for _ in range(20):
        alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=10.0, ask_qty=100.0, mid_price=101.0)
    assert alpha.get_signal() < 0.0


def test_symmetric_signal() -> None:
    """Swapping bid/ask qty should produce opposite-sign signals."""
    a_bid = MicropriceSkewAlpha()
    a_ask = MicropriceSkewAlpha()
    sig_bid = a_bid.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    sig_ask = a_ask.update(bid_px=100.0, ask_px=102.0, bid_qty=20.0, ask_qty=80.0, mid_price=101.0)
    assert sig_bid == pytest.approx(-sig_ask, abs=1e-9)


# ---------------------------------------------------------------------------
# Spread normalization
# ---------------------------------------------------------------------------


def test_normalized_skew_invariant_to_spread() -> None:
    """Normalized skew is invariant to spread for the same depth ratio.

    microprice - mid = spread * (bid_qty - ask_qty) / (2 * (bid_qty + ask_qty)),
    so (microprice - mid) / spread cancels the spread factor.
    """
    a_narrow = MicropriceSkewAlpha()
    a_wide = MicropriceSkewAlpha()
    # Narrow spread: 100-102 (spread=2)
    sig_narrow = a_narrow.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    # Wide spread: 100-110 (spread=10), same depth ratio
    sig_wide = a_wide.update(bid_px=100.0, ask_px=110.0, bid_qty=80.0, ask_qty=20.0, mid_price=105.0)
    assert sig_narrow == pytest.approx(sig_wide, abs=1e-9)


def test_zero_spread_no_crash() -> None:
    """Zero spread should not crash (uses epsilon guard)."""
    alpha = MicropriceSkewAlpha()
    sig = alpha.update(bid_px=100.0, ask_px=100.0, bid_qty=50.0, ask_qty=50.0, mid_price=100.0)
    assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# EMA correctness
# ---------------------------------------------------------------------------


def test_ema_first_step() -> None:
    """First update sets EMA directly to raw_skew (no smoothing)."""
    alpha = MicropriceSkewAlpha()
    bid_px, ask_px = 100.0, 102.0
    bid_qty, ask_qty = 80.0, 20.0
    mid_price = 101.0
    microprice = (ask_px * bid_qty + bid_px * ask_qty) / (bid_qty + ask_qty)
    spread = ask_px - bid_px
    expected_raw = (microprice - mid_price) / spread
    sig = alpha.update(bid_px=bid_px, ask_px=ask_px, bid_qty=bid_qty, ask_qty=ask_qty, mid_price=mid_price)
    assert sig == pytest.approx(expected_raw, abs=1e-9)


def test_ema_second_step() -> None:
    """Second update should apply EMA smoothing correctly."""
    alpha = MicropriceSkewAlpha()
    # First update: equal depths => raw_skew = 0
    alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    # Second update: bid-heavy
    bid_px, ask_px = 100.0, 102.0
    bid_qty, ask_qty = 80.0, 20.0
    mid_price = 101.0
    microprice = (ask_px * bid_qty + bid_px * ask_qty) / (bid_qty + ask_qty)
    spread = ask_px - bid_px
    raw_skew = (microprice - mid_price) / spread
    # EMA was 0 (from first step), so new_ema = 0 + alpha * (raw_skew - 0)
    expected = _EMA_ALPHA_8 * raw_skew
    sig = alpha.update(bid_px=bid_px, ask_px=ask_px, bid_qty=bid_qty, ask_qty=ask_qty, mid_price=mid_price)
    assert sig == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_kwargs() -> None:
    """update() accepts keyword arguments."""
    alpha = MicropriceSkewAlpha()
    sig = alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    assert math.isfinite(sig)


def test_positional() -> None:
    """update() accepts positional arguments."""
    alpha = MicropriceSkewAlpha()
    sig = alpha.update(100.0, 102.0, 50.0, 50.0, 101.0)
    assert math.isfinite(sig)


def test_partial_args_raises() -> None:
    """Fewer than 5 positional args should raise ValueError."""
    alpha = MicropriceSkewAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 102.0)
    with pytest.raises(ValueError):
        alpha.update(100.0, 102.0, 50.0, 50.0)


def test_reset() -> None:
    """reset() clears all state; next update acts as fresh start."""
    alpha = MicropriceSkewAlpha()
    alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # First update after reset should behave like a fresh instance
    alpha2 = MicropriceSkewAlpha()
    sig1 = alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    sig2 = alpha2.update(bid_px=100.0, ask_px=102.0, bid_qty=50.0, ask_qty=50.0, mid_price=101.0)
    assert sig1 == sig2


def test_get_signal() -> None:
    """get_signal() returns last computed signal."""
    alpha = MicropriceSkewAlpha()
    ret = alpha.update(bid_px=100.0, ask_px=102.0, bid_qty=80.0, ask_qty=20.0, mid_price=101.0)
    assert alpha.get_signal() == ret


def test_bounded() -> None:
    """Signal should remain finite under random input."""
    import numpy as np

    alpha = MicropriceSkewAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        bid = rng.integers(9900, 10000)
        ask = bid + rng.integers(1, 100)
        bq = rng.integers(1, 500)
        aq = rng.integers(1, 500)
        mid = (bid + ask) / 2.0
        sig = alpha.update(float(bid), float(ask), float(bq), float(aq), mid)
        assert math.isfinite(sig)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MicropriceSkewAlpha()
    assert isinstance(alpha, AlphaProtocol)

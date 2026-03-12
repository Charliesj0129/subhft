"""Unit tests for cross_venue_liquidity signal logic (16 tests).

Tests cover manifest integrity, EMA convergence, signal bounds,
state management, recovery tracking, and API compatibility.
"""
from __future__ import annotations

import math

from research.alphas.cross_venue_liquidity.impl import (
    CrossVenueLiquidityAlpha,
    _MANIFEST,
    _RECOVERY_ALPHA,
    _SIGNAL_ALPHA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alpha() -> CrossVenueLiquidityAlpha:
    a = CrossVenueLiquidityAlpha()
    a.reset()
    return a


def _warmup(alpha: CrossVenueLiquidityAlpha, n: int = 200, bid: float = 100.0, ask: float = 100.0) -> None:
    for _ in range(n):
        alpha.update(bid, ask)


# ---------------------------------------------------------------------------
# 1-5: Manifest integrity
# ---------------------------------------------------------------------------

def test_manifest_alpha_id() -> None:
    assert _MANIFEST.alpha_id == "cross_venue_liquidity"


def test_manifest_paper_refs_contains_062() -> None:
    assert "062" in _MANIFEST.paper_refs


def test_manifest_data_fields() -> None:
    assert "bid_qty" in _MANIFEST.data_fields
    assert "ask_qty" in _MANIFEST.data_fields


def test_manifest_complexity() -> None:
    assert _MANIFEST.complexity == "O(1)"


def test_manifest_feature_set_version() -> None:
    assert _MANIFEST.feature_set_version == "lob_shared_v1"


def test_manifest_roles_used_non_empty() -> None:
    assert len(_MANIFEST.roles_used) > 0


def test_manifest_skills_used_non_empty() -> None:
    assert len(_MANIFEST.skills_used) > 0


# ---------------------------------------------------------------------------
# 6-8: Cold start and basic signal
# ---------------------------------------------------------------------------

def test_cold_start_returns_zero() -> None:
    """First tick should return 0.0 (initialization, no prior state)."""
    alpha = _make_alpha()
    result = alpha.update(100.0, 100.0)
    assert result == 0.0


def test_initial_signal_is_zero_before_update() -> None:
    alpha = _make_alpha()
    assert alpha.get_signal() == 0.0


def test_balanced_constant_recovery_signal_near_zero() -> None:
    """Equal constant bid and ask quantities -> no recovery difference -> signal -> 0."""
    alpha = _make_alpha()
    _warmup(alpha, 300)
    assert abs(alpha.get_signal()) < 0.05


# ---------------------------------------------------------------------------
# 9-10: EMA convergence
# ---------------------------------------------------------------------------

def test_ema_converges_with_consistent_bid_recovery() -> None:
    """Steadily increasing bid qty (constant recovery) should converge."""
    alpha = _make_alpha()
    for i in range(500):
        alpha.update(100.0 + i * 1.0, 100.0)
    s1 = alpha.update(600.0, 100.0)
    s2 = alpha.update(601.0, 100.0)
    # With constant recovery rate, signal should stabilize
    assert abs(s2 - s1) < 0.01, f"EMA did not converge: s1={s1}, s2={s2}"


def test_ema_alpha_coefficients() -> None:
    assert abs(_RECOVERY_ALPHA - (1.0 - math.exp(-1.0 / 4.0))) < 1e-12
    assert abs(_SIGNAL_ALPHA - (1.0 - math.exp(-1.0 / 8.0))) < 1e-12


# ---------------------------------------------------------------------------
# 11-12: Signal direction — recovery asymmetry
# ---------------------------------------------------------------------------

def test_bid_recovery_dominant_gives_positive_signal() -> None:
    """When bid qty increases frequently but ask stays flat, signal should be positive."""
    alpha = _make_alpha()
    for i in range(300):
        # Bid keeps replenishing (going up), ask stays constant
        bid = 100.0 + (i % 10) * 10.0  # oscillates 100-190
        ask = 100.0  # constant = no recovery
        alpha.update(bid, ask)
    assert alpha.get_signal() > 0.0, f"Expected positive signal, got {alpha.get_signal()}"


def test_ask_recovery_dominant_gives_negative_signal() -> None:
    """When ask qty increases frequently but bid stays flat, signal should be negative."""
    alpha = _make_alpha()
    for i in range(300):
        bid = 100.0  # constant = no recovery
        ask = 100.0 + (i % 10) * 10.0  # oscillates 100-190
        alpha.update(bid, ask)
    assert alpha.get_signal() < 0.0, f"Expected negative signal, got {alpha.get_signal()}"


# ---------------------------------------------------------------------------
# 13-14: Signal bounds and state management
# ---------------------------------------------------------------------------

def test_signal_bounded_within_minus_1_to_plus_1() -> None:
    """Signal must stay in [-1.0, 1.0] for any input pattern."""
    import numpy as np

    alpha = _make_alpha()
    rng = np.random.default_rng(42)
    for _ in range(1000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        s = alpha.update(bid, ask)
        assert -1.0 <= s <= 1.0, f"Signal out of bounds: {s}"


def test_reset_clears_all_state() -> None:
    alpha = _make_alpha()
    for i in range(100):
        alpha.update(100.0 + i, 50.0)
    assert alpha.get_signal() != 0.0
    alpha.reset()
    assert alpha._bid_recovery_ema == 0.0
    assert alpha._ask_recovery_ema == 0.0
    assert alpha._signal_ema == 0.0
    assert alpha._signal == 0.0
    assert alpha._initialized is False


def test_get_signal_returns_last_update_value() -> None:
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init tick
    result = alpha.update(150.0, 100.0)
    assert result == alpha.get_signal()


def test_manifest_property_returns_correct_manifest() -> None:
    alpha = _make_alpha()
    assert alpha.manifest is _MANIFEST
    assert alpha.manifest.alpha_id == "cross_venue_liquidity"


# ---------------------------------------------------------------------------
# 15-16: Positional and keyword API compatibility
# ---------------------------------------------------------------------------

def test_positional_api() -> None:
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init
    r = alpha.update(150.0, 100.0)
    assert math.isfinite(r)


def test_keyword_api_bid_ask_qty() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    # Init tick
    alpha1.update(100.0, 100.0)
    alpha2.update(bid_qty=100.0, ask_qty=100.0)
    # Second tick
    r1 = alpha1.update(150.0, 100.0)
    r2 = alpha2.update(bid_qty=150.0, ask_qty=100.0)
    assert abs(r1 - r2) < 1e-12


def test_positional_and_keyword_equivalent() -> None:
    """update(x, y) == update(bid_qty=x, ask_qty=y) for same state."""
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    inputs = [(100.0, 80.0), (120.0, 60.0), (90.0, 110.0), (130.0, 70.0)]
    for b, a in inputs:
        alpha1.update(b, a)
        alpha2.update(bid_qty=b, ask_qty=a)
    assert abs(alpha1.get_signal() - alpha2.get_signal()) < 1e-12

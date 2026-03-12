"""Gate B correctness tests for DepthShockAlpha (ref 080)."""

from __future__ import annotations

import numpy as np
import pytest

from research.alphas.depth_shock.impl import (
    _A4,
    _A32,
    _EPSILON,
    ALPHA_CLASS,
    DepthShockAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert DepthShockAlpha().manifest.alpha_id == "depth_shock"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert DepthShockAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_080() -> None:
    assert "080" in DepthShockAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = DepthShockAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert DepthShockAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert DepthShockAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = DepthShockAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthShockAlpha


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = DepthShockAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# First tick returns zero
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First update must return 0.0 (no previous data to compute delta)."""
    alpha = DepthShockAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == 0.0


def test_first_tick_any_values_returns_zero() -> None:
    """First tick returns 0.0 regardless of input magnitude."""
    alpha = DepthShockAlpha()
    sig = alpha.update(999999.0, 1.0)
    assert sig == 0.0


# ---------------------------------------------------------------------------
# Signal direction: bid drop -> negative, ask drop -> positive
# ---------------------------------------------------------------------------


def test_bid_drop_gives_negative_signal() -> None:
    """Bid depth dropping (someone hitting bid) -> bearish -> negative signal."""
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)  # init tick
    # Bid drops sharply, ask stays
    for _ in range(30):
        alpha.update(50.0, 200.0)
        alpha.update(200.0, 200.0)  # recover, then drop again
    alpha.update(50.0, 200.0)
    assert alpha.get_signal() < 0.0, f"Expected negative, got {alpha.get_signal()}"


def test_ask_drop_gives_positive_signal() -> None:
    """Ask depth dropping (someone lifting ask) -> bullish -> positive signal."""
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)  # init tick
    # Ask drops sharply, bid stays
    for _ in range(30):
        alpha.update(200.0, 50.0)
        alpha.update(200.0, 200.0)  # recover, then drop again
    alpha.update(200.0, 50.0)
    assert alpha.get_signal() > 0.0, f"Expected positive, got {alpha.get_signal()}"


def test_symmetric_drops_cancel_out() -> None:
    """Equal drops on both sides -> shock = 0 -> signal near zero."""
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)
    for _ in range(50):
        alpha.update(100.0, 100.0)
        alpha.update(200.0, 200.0)
    assert abs(alpha.get_signal()) < 0.5


# ---------------------------------------------------------------------------
# Signal bounds [-2, 2]
# ---------------------------------------------------------------------------


def test_signal_bounded_minus_2_to_plus_2() -> None:
    """Signal must always be in [-2, 2]."""
    alpha = DepthShockAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(float(b), float(a))
        assert -2.0 <= sig <= 2.0, f"Signal out of bounds: {sig}"


def test_extreme_one_sided_clipped_at_bounds() -> None:
    """Extreme one-sided drops should clip at -2 or 2."""
    alpha = DepthShockAlpha()
    alpha.update(1e6, 100.0)
    # Massive bid drop
    for _ in range(100):
        alpha.update(1.0, 100.0)
        alpha.update(1e6, 100.0)
    alpha.update(1.0, 100.0)
    sig = alpha.get_signal()
    assert sig >= -2.0
    assert sig <= 2.0


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_converges_constant_shock() -> None:
    """With constant repeated shock, EMAs converge."""
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)
    # Repeated pattern: bid drops 100, ask stays -> shock = -100
    signals: list[float] = []
    for _ in range(200):
        alpha.update(100.0, 200.0)
        signals.append(alpha.get_signal())
        alpha.update(200.0, 200.0)
    # Signal should converge (last 10 values stable)
    last_10 = signals[-10:]
    assert max(last_10) - min(last_10) < 0.1


def test_ema_single_step_math() -> None:
    """Verify EMA update math for second tick."""
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)  # first tick -> init prev
    sig = alpha.update(100.0, 200.0)  # d_bid=-100, d_ask=0, shock = -100

    # After first real shock:
    # shock_ema = 0 + A4 * (-100 - 0) = -A4 * 100
    # shock_baseline = 0 + A32 * (100 - 0) = A32 * 100
    expected_ema = _A4 * -100.0
    expected_baseline = _A32 * 100.0
    expected_raw = expected_ema / max(expected_baseline, _EPSILON)
    expected_signal = max(-2.0, min(2.0, expected_raw))
    assert sig == pytest.approx(expected_signal, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = DepthShockAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert sig == 0.0  # first tick always 0


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = DepthShockAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert sig == 0.0  # first tick


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = DepthShockAlpha()
    alpha.update(200.0, 200.0)
    alpha.update(50.0, 200.0)  # create some state
    alpha.reset()
    # After reset, first update returns 0 (re-initializes)
    sig = alpha.update(300.0, 300.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = DepthShockAlpha()
    assert alpha.get_signal() == 0.0

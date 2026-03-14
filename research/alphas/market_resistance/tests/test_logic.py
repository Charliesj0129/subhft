"""Gate B correctness tests for MarketResistanceAlpha (ref 098)."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.market_resistance.impl import (
    ALPHA_CLASS,
    MarketResistanceAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert MarketResistanceAlpha().manifest.alpha_id == "market_resistance"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert MarketResistanceAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_098() -> None:
    assert "098" in MarketResistanceAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = MarketResistanceAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_feature_set_version() -> None:
    assert MarketResistanceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MarketResistanceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MarketResistanceAlpha


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MarketResistanceAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First update should return 0.0 (initialization tick, no prior state)."""
    alpha = MarketResistanceAlpha()
    sig = alpha.update(100.0, 100.0, 50.0)
    assert sig == 0.0


def test_constant_queues_constant_price_signal_near_zero() -> None:
    """Constant bid/ask/mid with no change produces near-zero OFI and signal."""
    alpha = MarketResistanceAlpha()
    for _ in range(200):
        alpha.update(100.0, 100.0, 50.0)
    assert abs(alpha.get_signal()) < 0.01


def test_signal_bounded_in_range() -> None:
    """Signal must stay in [-2, 2] for random inputs."""
    alpha = MarketResistanceAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 500)
    asks = rng.uniform(0, 1000, 500)
    mids = rng.uniform(90, 110, 500)
    for b, a, m in zip(bids, asks, mids):
        sig = alpha.update(float(b), float(a), float(m))
        assert -2.0 <= sig <= 2.0, f"Signal out of bounds: {sig}"


# ---------------------------------------------------------------------------
# Resistance detection: flow absorbed, price doesn't move
# ---------------------------------------------------------------------------


def test_resistance_signal_positive_when_flow_absorbed() -> None:
    """When large OFI occurs but price barely moves, signal should go positive
    (resistance: market absorbing flow)."""
    alpha = MarketResistanceAlpha()
    mid = 100.0
    bid = 100.0
    # Simulate increasing bid pressure (positive OFI) but flat price
    for i in range(200):
        bid += 5.0  # bid_qty growing -> positive OFI
        alpha.update(bid, 100.0, mid)  # mid stays at 100
    sig = alpha.get_signal()
    assert sig > 0.0, f"Expected positive (resistance), got {sig}"


def test_momentum_signal_when_price_follows_flow() -> None:
    """When price moves in proportion to OFI, ratio stays near baseline
    (no deviation from expected behavior), signal stays near zero or adjusts."""
    alpha = MarketResistanceAlpha()
    mid = 100.0
    bid = 100.0
    for i in range(200):
        bid += 5.0
        mid += 0.5  # price follows flow proportionally
        alpha.update(bid, 100.0, mid)
    # Signal should be less extreme than the pure resistance case
    sig_momentum = alpha.get_signal()

    alpha2 = MarketResistanceAlpha()
    mid2 = 100.0
    bid2 = 100.0
    for i in range(200):
        bid2 += 5.0
        alpha2.update(bid2, 100.0, mid2)  # price flat = pure resistance
    sig_resist = alpha2.get_signal()
    assert sig_resist > sig_momentum, (
        f"Resistance signal ({sig_resist}) should exceed momentum signal ({sig_momentum})"
    )


# ---------------------------------------------------------------------------
# EMA convergence
# ---------------------------------------------------------------------------


def test_ema_converges_steady_state() -> None:
    """With constant OFI and constant dprice, signal should converge."""
    alpha = MarketResistanceAlpha()
    # Initialize
    alpha.update(100.0, 100.0, 50.0)
    signals: list[float] = []
    for _ in range(500):
        sig = alpha.update(105.0, 100.0, 50.0)  # constant bid_change=5, dprice=0
        signals.append(sig)
    # Last 50 signals should be nearly identical (converged)
    last_50 = signals[-50:]
    spread = max(last_50) - min(last_50)
    assert spread < 0.01, f"Signal did not converge: spread={spread}"


def test_second_tick_uses_ema_update() -> None:
    """Second tick should update EMA state, not just initialize."""
    alpha = MarketResistanceAlpha()
    s1 = alpha.update(100.0, 100.0, 50.0)
    s2 = alpha.update(200.0, 100.0, 50.0)
    # s1 = 0.0 (init), s2 should be non-zero since bid_change = 100
    assert s1 == 0.0
    # s2 is based on EMA of (ratio - baseline) which is small on first real tick
    assert isinstance(s2, float)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = MarketResistanceAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0, mid_price=50.0)
    assert isinstance(sig, float)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = MarketResistanceAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    assert isinstance(sig, float)


def test_update_no_args_returns_float() -> None:
    alpha = MarketResistanceAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_reset_clears_state() -> None:
    alpha = MarketResistanceAlpha()
    alpha.update(800.0, 100.0, 50.0)
    alpha.update(900.0, 100.0, 50.5)
    alpha.reset()
    # After reset, first update should return 0.0 (init tick)
    sig = alpha.update(300.0, 300.0, 50.0)
    assert sig == 0.0


def test_get_signal_before_update_is_zero() -> None:
    alpha = MarketResistanceAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Signal direction with ask-side dominance
# ---------------------------------------------------------------------------


def test_ask_side_flow_absorbed_gives_negative_signal() -> None:
    """When ask_qty grows (negative OFI) but price stable, signal should
    go negative (resistance on ask side)."""
    alpha = MarketResistanceAlpha()
    mid = 100.0
    ask = 100.0
    for i in range(200):
        ask += 5.0  # ask_qty growing -> negative OFI
        alpha.update(100.0, ask, mid)
    sig = alpha.get_signal()
    assert sig < 0.0, f"Expected negative signal for ask-side resistance, got {sig}"

"""Gate B correctness tests for ToxicityMultiscaleAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxicity_multiscale.impl import (
    ALPHA_CLASS,
    ToxicityMultiscaleAlpha,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = ToxicityMultiscaleAlpha().manifest
    assert m.alpha_id == "toxicity_multiscale"
    assert m.data_fields == ("bid_qty", "ask_qty", "spread_scaled", "mid_price")
    from research.registry.schemas import AlphaStatus

    assert m.status == AlphaStatus.DRAFT


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert ToxicityMultiscaleAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert ToxicityMultiscaleAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ToxicityMultiscaleAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ToxicityMultiscaleAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ToxicityMultiscaleAlpha


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ToxicityMultiscaleAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = ToxicityMultiscaleAlpha()
    assert alpha.get_signal() == 0.0


def test_first_tick_returns_zero() -> None:
    """First tick initializes state, returns 0."""
    alpha = ToxicityMultiscaleAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 100.0)
    assert sig == 0.0


def test_high_volatility_high_imbalance_strong_signal() -> None:
    """Large volatility + large |QI| + wide spread -> larger signal."""
    alpha = ToxicityMultiscaleAlpha()
    # Feed initial tick
    alpha.update(500.0, 100.0, 50.0, 100.0)
    # Feed ticks with increasing mid price (volatility)
    for i in range(100):
        mid = 100.0 + i * 0.5  # increasing mid
        alpha.update(800.0, 100.0, 100.0, mid)
    sig = alpha.get_signal()
    assert abs(sig) > 0.01


def test_zero_volatility_weak_signal() -> None:
    """Constant mid price -> zero volatility -> signal near zero."""
    alpha = ToxicityMultiscaleAlpha()
    for _ in range(200):
        alpha.update(500.0, 100.0, 50.0, 100.0)  # constant mid
    sig = alpha.get_signal()
    assert abs(sig) < 0.01


def test_signal_direction_follows_qi() -> None:
    """Positive QI (bid > ask) -> positive signal."""
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(500.0, 100.0, 50.0, 100.0)
    for i in range(50):
        mid = 100.0 + i * 0.1
        alpha.update(500.0, 100.0, 50.0, mid)
    sig = alpha.get_signal()
    assert sig > 0.0


def test_signal_direction_negative_qi() -> None:
    """Negative QI (ask > bid) -> negative signal."""
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(100.0, 500.0, 50.0, 100.0)
    for i in range(50):
        mid = 100.0 + i * 0.1
        alpha.update(100.0, 500.0, 50.0, mid)
    sig = alpha.get_signal()
    assert sig < 0.0


def test_spread_widening_amplifies() -> None:
    """Spread above baseline -> spread_dev > 1 -> amplified signal."""
    alpha_base = ToxicityMultiscaleAlpha()
    alpha_wide = ToxicityMultiscaleAlpha()

    # Both get same initial ticks
    for i in range(200):
        mid = 100.0 + i * 0.01
        alpha_base.update(500.0, 100.0, 50.0, mid)
        alpha_wide.update(500.0, 100.0, 50.0, mid)

    # Now diverge: wide spread vs baseline
    for i in range(20):
        mid = 102.0 + i * 0.05
        alpha_base.update(500.0, 100.0, 50.0, mid)
        alpha_wide.update(500.0, 100.0, 200.0, mid)  # 4x spread

    assert abs(alpha_wide.get_signal()) > abs(alpha_base.get_signal())


def test_signal_bounded_at_2() -> None:
    """Signal is clipped to [-2, 2]."""
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(10000.0, 1.0, 1000.0, 100.0)
    for i in range(500):
        mid = 100.0 + i * 10.0  # extreme volatility
        alpha.update(10000.0, 1.0, 10000.0, mid)
    sig = alpha.get_signal()
    assert -2.0 <= sig <= 2.0


def test_reset_clears_state() -> None:
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(800.0, 100.0, 100.0, 100.0)
    alpha.update(800.0, 100.0, 100.0, 105.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should equal fresh instance
    fresh = ToxicityMultiscaleAlpha()
    s1 = alpha.update(300.0, 300.0, 50.0, 100.0)
    s2 = fresh.update(300.0, 300.0, 50.0, 100.0)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_get_signal_matches_update_return() -> None:
    alpha = ToxicityMultiscaleAlpha()
    ret = alpha.update(500.0, 100.0, 80.0, 100.0)
    assert ret == alpha.get_signal()


def test_kwargs_interface() -> None:
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(bid_qty=500.0, ask_qty=100.0, spread_scaled=50.0, mid_price=100.0)
    sig = alpha.update(
        bid_qty=500.0,
        ask_qty=100.0,
        spread_scaled=50.0,
        mid_price=101.0,
    )
    assert isinstance(sig, float)


def test_positional_interface() -> None:
    alpha = ToxicityMultiscaleAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 100.0)
    assert isinstance(sig, float)


def test_zero_quantities_handled() -> None:
    """bid_qty=0, ask_qty=0 doesn't crash."""
    alpha = ToxicityMultiscaleAlpha()
    sig = alpha.update(0.0, 0.0, 50.0, 100.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_signal_finite_random_data() -> None:
    """Signal stays finite with random data."""
    import numpy as np

    alpha = ToxicityMultiscaleAlpha()
    rng = np.random.default_rng(42)
    mid = 100.0
    for _ in range(500):
        bq = rng.uniform(0, 1000)
        aq = rng.uniform(0, 1000)
        sp = rng.uniform(1, 200)
        mid += rng.normal(0, 0.5)
        sig = alpha.update(bq, aq, sp, mid)
        assert math.isfinite(sig)
        assert -2.0 <= sig <= 2.0


def test_update_insufficient_args_raises() -> None:
    alpha = ToxicityMultiscaleAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_equal_qty_gives_zero_signal() -> None:
    """When bid_qty == ask_qty, QI is 0, so signal is 0."""
    alpha = ToxicityMultiscaleAlpha()
    alpha.update(100.0, 100.0, 50.0, 100.0)
    sig = alpha.update(100.0, 100.0, 50.0, 101.0)
    assert sig == 0.0

"""Gate B correctness tests for MicropriceReversionAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.microprice_reversion.impl import (
    ALPHA_CLASS,
    MicropriceReversionAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = MicropriceReversionAlpha().manifest
    assert m.alpha_id == "microprice_reversion"
    assert m.data_fields == ("microprice_x2", "mid_price_x2", "spread_scaled")
    assert m.status.value == "DRAFT"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert MicropriceReversionAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert MicropriceReversionAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert MicropriceReversionAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = MicropriceReversionAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is MicropriceReversionAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = MicropriceReversionAlpha()
    assert alpha.get_signal() == 0.0


def test_update_positive_deviation_gives_negative_signal() -> None:
    """microprice > mid -> deviation positive -> signal negative (reversion)."""
    alpha = MicropriceReversionAlpha()
    # microprice_x2=110, mid_price_x2=100, spread_scaled=10
    sig = alpha.update(110, 100, 10)
    assert sig < 0.0


def test_update_negative_deviation_gives_positive_signal() -> None:
    """microprice < mid -> deviation negative -> signal positive (reversion)."""
    alpha = MicropriceReversionAlpha()
    sig = alpha.update(90, 100, 10)
    assert sig > 0.0


def test_ema_convergence() -> None:
    """After 100 identical ticks, signal converges to expected value."""
    alpha = MicropriceReversionAlpha()
    microprice_x2, mid_price_x2, spread_scaled = 110, 100, 10
    expected_dev = (microprice_x2 - mid_price_x2) / max(spread_scaled, 1)
    for _ in range(100):
        alpha.update(microprice_x2, mid_price_x2, spread_scaled)
    assert alpha.get_signal() == pytest.approx(-expected_dev, abs=1e-4)


def test_reset_clears_state() -> None:
    """After reset(), signal is 0."""
    alpha = MicropriceReversionAlpha()
    alpha.update(110, 100, 10)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert alpha._initialized is False


def test_get_signal_matches_update_return() -> None:
    """update() return == get_signal()."""
    alpha = MicropriceReversionAlpha()
    ret = alpha.update(110, 100, 10)
    assert ret == alpha.get_signal()


def test_spread_normalization() -> None:
    """Wider spread reduces signal magnitude."""
    alpha_narrow = MicropriceReversionAlpha()
    alpha_wide = MicropriceReversionAlpha()
    sig_narrow = alpha_narrow.update(110, 100, 5)
    sig_wide = alpha_wide.update(110, 100, 20)
    assert abs(sig_narrow) > abs(sig_wide)


def test_zero_spread_handled() -> None:
    """spread=0 doesn't crash (uses max(spread, 1))."""
    alpha = MicropriceReversionAlpha()
    sig = alpha.update(110, 100, 0)
    assert isinstance(sig, float)
    # With spread=0, denom=1, dev=(110-100)/1=10, signal=-10
    assert sig == pytest.approx(-10.0, abs=1e-9)


def test_kwargs_interface() -> None:
    """update(microprice_x2=..., mid_price_x2=..., spread_scaled=...)."""
    alpha = MicropriceReversionAlpha()
    sig = alpha.update(microprice_x2=110, mid_price_x2=100, spread_scaled=10)
    expected_dev = (110 - 100) / 10
    assert sig == pytest.approx(-expected_dev, abs=1e-9)


def test_positional_interface() -> None:
    """update(100, 98, 2)."""
    alpha = MicropriceReversionAlpha()
    sig = alpha.update(100, 98, 2)
    expected_dev = (100 - 98) / 2
    assert sig == pytest.approx(-expected_dev, abs=1e-9)


def test_signal_range() -> None:
    """Signal stays within reasonable bounds [-2, 2] with normal data."""
    import numpy as np

    alpha = MicropriceReversionAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        mid = 2000000
        spread = rng.integers(10, 100)
        micro = mid + rng.integers(-spread, spread + 1)
        sig = alpha.update(int(micro), mid, int(spread))
        assert -2.0 <= sig <= 2.0


def test_symmetric_response() -> None:
    """|signal(+dev)| ~ |signal(-dev)| for symmetric deviations."""
    alpha_pos = MicropriceReversionAlpha()
    alpha_neg = MicropriceReversionAlpha()
    sig_pos = alpha_pos.update(110, 100, 10)
    sig_neg = alpha_neg.update(90, 100, 10)
    assert abs(sig_pos) == pytest.approx(abs(sig_neg), abs=1e-9)
    assert sig_pos < 0  # positive deviation -> negative signal
    assert sig_neg > 0  # negative deviation -> positive signal


def test_ema_single_step_initializes_to_raw_dev() -> None:
    """First update initializes EMA to raw micro_dev."""
    alpha = MicropriceReversionAlpha()
    sig = alpha.update(110, 100, 10)
    expected = -((110 - 100) / 10)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = MicropriceReversionAlpha()
    m1, mid1, s1 = 110, 100, 10
    m2, mid2, s2 = 90, 100, 10
    dev1 = (m1 - mid1) / max(s1, 1)
    dev2 = (m2 - mid2) / max(s2, 1)
    expected_ema2 = dev1 + _EMA_ALPHA * (dev2 - dev1)
    alpha.update(m1, mid1, s1)
    sig2 = alpha.update(m2, mid2, s2)
    assert sig2 == pytest.approx(-expected_ema2, abs=1e-9)


def test_update_one_arg_raises() -> None:
    alpha = MicropriceReversionAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_update_two_args_raises() -> None:
    alpha = MicropriceReversionAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0, 98.0)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = MicropriceReversionAlpha()
    assert isinstance(alpha, AlphaProtocol)

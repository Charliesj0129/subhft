"""Gate B correctness tests for ToxicFlowAlpha."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.toxic_flow.impl import (
    ALPHA_CLASS,
    ToxicFlowAlpha,
    _EMA_ALPHA_8,
    _EMA_ALPHA_64,
    _EPSILON,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_fields() -> None:
    m = ToxicFlowAlpha().manifest
    assert m.alpha_id == "toxic_flow"
    assert m.data_fields == ("bid_qty", "ask_qty", "spread_scaled", "ofi_l1_ema8")
    from research.registry.schemas import AlphaStatus

    assert m.status == AlphaStatus.DRAFT


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert ToxicFlowAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_latency_profile_set() -> None:
    assert ToxicFlowAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ToxicFlowAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = ToxicFlowAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ToxicFlowAlpha


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = ToxicFlowAlpha()
    assert isinstance(alpha, AlphaProtocol)


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_initial_signal_zero() -> None:
    """Signal is 0 before first update."""
    alpha = ToxicFlowAlpha()
    assert alpha.get_signal() == 0.0


def test_high_imbalance_wide_spread_strong_signal() -> None:
    """Large |qi| + wide spread -> large |signal|."""
    alpha = ToxicFlowAlpha()
    # Feed many ticks with high imbalance (bid >> ask) and wide spread
    for _ in range(50):
        sig = alpha.update(1000.0, 10.0, 200.0, 1.0)
    assert abs(sig) > 0.5


def test_low_imbalance_narrow_spread_weak_signal() -> None:
    """Small |qi| + narrow spread -> near-zero signal."""
    alpha = ToxicFlowAlpha()
    for _ in range(50):
        sig = alpha.update(100.0, 99.0, 10.0, 0.01)
    assert abs(sig) < 0.1


def test_signal_direction_follows_ofi() -> None:
    """Positive ofi_ema8 -> positive signal."""
    alpha = ToxicFlowAlpha()
    for _ in range(30):
        sig = alpha.update(500.0, 100.0, 50.0, 1.0)
    assert sig > 0.0


def test_signal_direction_negative_ofi() -> None:
    """Negative ofi_ema8 -> negative signal."""
    alpha = ToxicFlowAlpha()
    for _ in range(30):
        sig = alpha.update(100.0, 500.0, 50.0, -1.0)
    assert sig < 0.0


def test_spread_widening_amplifies() -> None:
    """Spread > EMA_64(spread) -> spread_norm > 1 -> amplified signal."""
    alpha = ToxicFlowAlpha()
    # Establish a baseline spread of 50
    for _ in range(200):
        alpha.update(500.0, 100.0, 50.0, 1.0)
    baseline_signal = alpha.get_signal()

    # Now widen spread to 200 (4x baseline) — should amplify
    alpha_wide = ToxicFlowAlpha()
    for _ in range(200):
        alpha_wide.update(500.0, 100.0, 50.0, 1.0)
    # Feed a few ticks with wider spread
    for _ in range(10):
        alpha_wide.update(500.0, 100.0, 200.0, 1.0)
    wide_signal = alpha_wide.get_signal()

    assert wide_signal > baseline_signal


def test_spread_narrowing_dampens() -> None:
    """Spread < EMA_64(spread) -> spread_norm < 1 -> dampened signal."""
    alpha = ToxicFlowAlpha()
    # Establish baseline spread of 100
    for _ in range(200):
        alpha.update(500.0, 100.0, 100.0, 1.0)
    baseline_signal = alpha.get_signal()

    # Now narrow spread to 10 — should dampen
    alpha_narrow = ToxicFlowAlpha()
    for _ in range(200):
        alpha_narrow.update(500.0, 100.0, 100.0, 1.0)
    for _ in range(10):
        alpha_narrow.update(500.0, 100.0, 10.0, 1.0)
    narrow_signal = alpha_narrow.get_signal()

    assert narrow_signal < baseline_signal


def test_ema_convergence() -> None:
    """After 200 identical ticks, signal converges to a stable value."""
    alpha = ToxicFlowAlpha()
    for _ in range(200):
        alpha.update(300.0, 100.0, 50.0, 1.0)
    sig_a = alpha.get_signal()
    for _ in range(50):
        alpha.update(300.0, 100.0, 50.0, 1.0)
    sig_b = alpha.get_signal()
    assert sig_a == pytest.approx(sig_b, abs=1e-6)


def test_reset_clears_state() -> None:
    alpha = ToxicFlowAlpha()
    alpha.update(800.0, 100.0, 100.0, 1.0)
    alpha.reset()
    assert alpha.get_signal() == 0.0
    # After reset, first update should equal fresh instance
    fresh = ToxicFlowAlpha()
    s1 = alpha.update(300.0, 300.0, 50.0, 0.5)
    s2 = fresh.update(300.0, 300.0, 50.0, 0.5)
    assert s1 == pytest.approx(s2, abs=1e-9)


def test_get_signal_matches_update_return() -> None:
    alpha = ToxicFlowAlpha()
    ret = alpha.update(500.0, 100.0, 80.0, 1.0)
    assert ret == alpha.get_signal()


def test_kwargs_interface() -> None:
    alpha = ToxicFlowAlpha()
    sig = alpha.update(
        bid_qty=500.0,
        ask_qty=100.0,
        spread_scaled=50.0,
        ofi_l1_ema8=1.0,
    )
    assert isinstance(sig, float)
    assert sig != 0.0


def test_positional_interface() -> None:
    alpha = ToxicFlowAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 1.0)
    assert isinstance(sig, float)
    assert sig != 0.0


def test_zero_quantities_handled() -> None:
    """bid_qty=0, ask_qty=0 doesn't crash (epsilon guards division)."""
    alpha = ToxicFlowAlpha()
    sig = alpha.update(0.0, 0.0, 50.0, 0.0)
    assert isinstance(sig, float)
    assert math.isfinite(sig)


def test_signal_bounded() -> None:
    """Signal stays in reasonable range with normal data."""
    import numpy as np

    alpha = ToxicFlowAlpha()
    rng = np.random.default_rng(42)
    for _ in range(500):
        bq = rng.uniform(0, 1000)
        aq = rng.uniform(0, 1000)
        sp = rng.uniform(1, 200)
        ofi = rng.uniform(-1, 1)
        sig = alpha.update(bq, aq, sp, ofi)
        assert math.isfinite(sig)
        # Toxicity EMA is bounded [0, spread_norm_max], signal = +/- that.
        # With normalized spreads and |QI| in [0,1], signal should be moderate.
        assert abs(sig) < 100.0


def test_update_insufficient_args_raises() -> None:
    alpha = ToxicFlowAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_zero_ofi_gives_zero_signal() -> None:
    """When ofi_l1_ema8 is exactly 0, sign is 0, so signal is 0."""
    alpha = ToxicFlowAlpha()
    sig = alpha.update(500.0, 100.0, 50.0, 0.0)
    assert sig == 0.0

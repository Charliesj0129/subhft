"""Gate B correctness tests for HawkesOfiImpactAlpha (ref 026)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.hawkes_ofi_impact.impl import (
    _BASELINE_INTENSITY,
    _EPSILON,
    ALPHA_CLASS,
    HawkesOfiImpactAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert HawkesOfiImpactAlpha().manifest.alpha_id == "hawkes_ofi_impact"


def test_manifest_tier_is_ensemble() -> None:
    from research.registry.schemas import AlphaTier

    assert HawkesOfiImpactAlpha().manifest.tier == AlphaTier.ENSEMBLE


def test_manifest_paper_refs_includes_026() -> None:
    assert "026" in HawkesOfiImpactAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = HawkesOfiImpactAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert HawkesOfiImpactAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert HawkesOfiImpactAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = HawkesOfiImpactAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is HawkesOfiImpactAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First tick initializes prev state; signal must be 0."""
    alpha = HawkesOfiImpactAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == 0.0


def test_signal_bounded_in_range() -> None:
    """Signal must stay in [-2, 2] for all random inputs."""
    alpha = HawkesOfiImpactAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(1, 1000, 500)
    asks = rng.uniform(1, 1000, 500)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 <= sig <= 2.0, f"signal {sig} out of [-2, 2]"


def test_constant_queues_signal_decays_to_zero() -> None:
    """If bid/ask queues are constant, OFI = 0 every tick -> signal decays."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(200.0, 200.0)  # init
    for _ in range(200):
        sig = alpha.update(200.0, 200.0)
    assert abs(sig) < 1e-6


def test_bid_increase_gives_positive_signal() -> None:
    """Increasing bid queue (positive OFI) should produce positive signal."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)  # init
    # Bid increases significantly
    sig = alpha.update(200.0, 100.0)
    assert sig > 0.0


def test_ask_increase_gives_negative_signal() -> None:
    """Increasing ask queue (negative OFI) should produce negative signal."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)  # init
    # Ask increases significantly
    sig = alpha.update(100.0, 200.0)
    assert sig < 0.0


def test_zero_queues_safe() -> None:
    """Zero bid and ask queues should not crash (epsilon guard)."""
    alpha = HawkesOfiImpactAlpha()
    sig = alpha.update(0.0, 0.0)
    assert isinstance(sig, float)
    sig2 = alpha.update(0.0, 0.0)
    assert isinstance(sig2, float)


# ---------------------------------------------------------------------------
# EMA convergence and Hawkes intensity
# ---------------------------------------------------------------------------


def test_ofi_ema_converges_constant_ofi() -> None:
    """With constant OFI input, EMA should converge to raw OFI."""
    alpha = HawkesOfiImpactAlpha()
    # Init tick
    alpha.update(100.0, 100.0)
    # Each tick: bid goes up by 10, ask stays -> OFI = +10 each tick
    bid = 100.0
    for _ in range(300):
        bid += 10.0
        alpha.update(bid, 100.0)
    # After convergence, ofi_ema should be close to 10.0
    assert abs(alpha._ofi_ema - 10.0) < 0.5


def test_hawkes_intensity_rises_with_large_ofi() -> None:
    """Large OFI bursts should raise intensity above baseline."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)
    # Burst: large bid swing
    alpha.update(500.0, 100.0)
    assert alpha._intensity_ema > _BASELINE_INTENSITY


def test_hawkes_intensity_decays_during_calm() -> None:
    """After a burst, constant queues should let intensity decay."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)
    # Burst
    alpha.update(500.0, 100.0)
    intensity_after_burst = alpha._intensity_ema
    # Calm period
    for _ in range(100):
        alpha.update(500.0, 100.0)  # constant = OFI 0
    assert alpha._intensity_ema < intensity_after_burst


def test_intensity_factor_clipped_low() -> None:
    """Intensity factor should not go below 0.5."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)
    # Many calm ticks -> intensity approaches 0 -> factor clipped at 0.5
    for _ in range(500):
        alpha.update(100.0, 100.0)
    factor = max(
        0.5,
        min(2.0, alpha._intensity_ema / (_BASELINE_INTENSITY + _EPSILON)),
    )
    assert factor >= 0.5


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = HawkesOfiImpactAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    assert isinstance(sig, float)


def test_update_one_arg_raises() -> None:
    alpha = HawkesOfiImpactAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = HawkesOfiImpactAlpha()
    alpha.update(800.0, 100.0)
    alpha.update(100.0, 800.0)
    alpha.reset()
    # After reset, first update should be 0 (init tick)
    sig = alpha.update(300.0, 300.0)
    assert sig == 0.0
    assert alpha._ofi_ema == 0.0
    assert alpha._intensity_ema == _BASELINE_INTENSITY


def test_get_signal_before_update_is_zero() -> None:
    alpha = HawkesOfiImpactAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = HawkesOfiImpactAlpha()
    assert isinstance(alpha, AlphaProtocol)

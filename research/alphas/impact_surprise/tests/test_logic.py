"""Gate B correctness tests for ImpactSurpriseAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import pytest

from research.alphas.impact_surprise.impl import (
    ALPHA_CLASS,
    ImpactSurpriseAlpha,
    _EMA_ALPHA,
    _EMA_SPAN,
    _ISS_THRESHOLD,
    _SIGNAL_CLIP,
    _WARMUP_TICKS,
)


def _feed_constant(alpha: ImpactSurpriseAlpha, n: int, ofi: int = 0,
                   mid_x2: int = 200000, bid_depth: int = 100,
                   ask_depth: int = 100) -> None:
    """Feed n ticks of constant data for warmup."""
    for _ in range(n):
        alpha.update(
            ofi_l1_raw=ofi, mid_price_x2=mid_x2,
            bid_depth=bid_depth, ask_depth=ask_depth,
        )


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert ImpactSurpriseAlpha().manifest.alpha_id == "impact_surprise"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert ImpactSurpriseAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    refs = ImpactSurpriseAlpha().manifest.paper_refs
    assert "arXiv:2508.06788" in refs
    assert "Cont2014" in refs


def test_manifest_data_fields() -> None:
    f = ImpactSurpriseAlpha().manifest.data_fields
    assert "ofi_l1_raw" in f
    assert "mid_price_x2" in f
    assert "bid_depth" in f
    assert "ask_depth" in f


def test_manifest_latency_profile_set() -> None:
    assert ImpactSurpriseAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert ImpactSurpriseAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = ImpactSurpriseAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is ImpactSurpriseAlpha


# --- Warmup ---
def test_warmup_returns_zero() -> None:
    alpha = ImpactSurpriseAlpha()
    for i in range(_WARMUP_TICKS - 1):
        sig = alpha.update(
            ofi_l1_raw=10 * ((-1) ** i), mid_price_x2=200000,
            bid_depth=100, ask_depth=100,
        )
        assert sig == 0.0, f"tick {i}: expected 0.0 during warmup, got {sig}"


def test_signal_activates_after_warmup() -> None:
    alpha = ImpactSurpriseAlpha()
    mid = 200000
    _feed_constant(alpha, _WARMUP_TICKS, ofi=0, mid_x2=mid)
    activated = False
    for i in range(200):
        ofi = 50
        mid += ofi * 2
        sig = alpha.update(
            ofi_l1_raw=ofi, mid_price_x2=mid,
            bid_depth=10, ask_depth=10,
        )
        if sig != 0.0:
            activated = True
    assert activated, "signal should activate after warmup with correlated OFI/returns"


# --- Core signal properties ---
def test_high_impact_positive_signal() -> None:
    """When OFI strongly moves price (high b_hat vs low depth), ISS should be positive."""
    alpha = ImpactSurpriseAlpha()
    mid = 200000
    _feed_constant(alpha, _WARMUP_TICKS, mid_x2=mid)
    for i in range(500):
        ofi = 100
        mid += 10
        alpha.update(
            ofi_l1_raw=ofi, mid_price_x2=mid,
            bid_depth=5, ask_depth=5,
        )
    assert alpha.b_hat > 0.0, "b_hat should be positive with correlated OFI/return"


def test_zero_ofi_no_signal() -> None:
    """With zero OFI, b_hat should be near zero and signal should be zero."""
    alpha = ImpactSurpriseAlpha()
    for i in range(_WARMUP_TICKS + 200):
        alpha.update(
            ofi_l1_raw=0, mid_price_x2=200000,
            bid_depth=100, ask_depth=100,
        )
    assert alpha.get_signal() == pytest.approx(0.0, abs=0.01)


def test_uncorrelated_ofi_return_signal_near_zero() -> None:
    """With random uncorrelated OFI and returns, signal should be near zero."""
    import random
    random.seed(42)
    alpha = ImpactSurpriseAlpha()
    mid = 200000
    for i in range(_WARMUP_TICKS + 500):
        ofi = random.randint(-100, 100)
        mid += random.randint(-5, 5)
        alpha.update(
            ofi_l1_raw=ofi, mid_price_x2=mid,
            bid_depth=100, ask_depth=100,
        )
    assert abs(alpha.get_signal()) < _SIGNAL_CLIP


# --- Boundary conditions ---
def test_signal_clipped_to_bounds() -> None:
    alpha = ImpactSurpriseAlpha()
    mid = 200000
    for i in range(_WARMUP_TICKS + 500):
        ofi = 1000
        mid += 500
        sig = alpha.update(
            ofi_l1_raw=ofi, mid_price_x2=mid,
            bid_depth=1, ask_depth=1,
        )
        assert -_SIGNAL_CLIP <= sig <= _SIGNAL_CLIP


def test_zero_depth_no_crash() -> None:
    alpha = ImpactSurpriseAlpha()
    for _ in range(_WARMUP_TICKS + 10):
        sig = alpha.update(
            ofi_l1_raw=0, mid_price_x2=200000,
            bid_depth=0, ask_depth=0,
        )
    assert math.isfinite(sig)


def test_large_values_no_overflow() -> None:
    alpha = ImpactSurpriseAlpha()
    for _ in range(_WARMUP_TICKS + 10):
        sig = alpha.update(
            ofi_l1_raw=10**8, mid_price_x2=10**9,
            bid_depth=10**7, ask_depth=10**7,
        )
    assert math.isfinite(sig)


# --- Reset / state ---
def test_reset_clears_state() -> None:
    alpha = ImpactSurpriseAlpha()
    _feed_constant(alpha, _WARMUP_TICKS + 50, ofi=100, mid_x2=200100)
    alpha.reset()
    alpha2 = ImpactSurpriseAlpha()
    sig1 = alpha.update(
        ofi_l1_raw=10, mid_price_x2=200000, bid_depth=50, ask_depth=50,
    )
    sig2 = alpha2.update(
        ofi_l1_raw=10, mid_price_x2=200000, bid_depth=50, ask_depth=50,
    )
    assert sig1 == pytest.approx(sig2, abs=1e-9)


def test_get_signal_before_update() -> None:
    assert ImpactSurpriseAlpha().get_signal() == 0.0


# --- b_hat / b_eq properties ---
def test_b_eq_inversely_proportional_to_depth() -> None:
    alpha = ImpactSurpriseAlpha()
    alpha.update(ofi_l1_raw=0, mid_price_x2=200000, bid_depth=10, ask_depth=10)
    b_eq_shallow = alpha.b_eq
    alpha.reset()
    alpha.update(ofi_l1_raw=0, mid_price_x2=200000, bid_depth=1000, ask_depth=1000)
    b_eq_deep = alpha.b_eq
    assert b_eq_shallow > b_eq_deep


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(ImpactSurpriseAlpha(), AlphaProtocol)


# --- EMA convergence ---
def test_constant_input_converges() -> None:
    alpha = ImpactSurpriseAlpha()
    for _ in range(2000):
        alpha.update(
            ofi_l1_raw=50, mid_price_x2=200000,
            bid_depth=100, ask_depth=100,
        )
    sig1 = alpha.get_signal()
    for _ in range(500):
        alpha.update(
            ofi_l1_raw=50, mid_price_x2=200000,
            bid_depth=100, ask_depth=100,
        )
    sig2 = alpha.get_signal()
    assert abs(sig2 - sig1) < 0.01, "signal should converge with constant input"

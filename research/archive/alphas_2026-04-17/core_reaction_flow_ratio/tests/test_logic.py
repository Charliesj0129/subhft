"""Gate B correctness tests for CoreReactionFlowRatioAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.core_reaction_flow_ratio.impl import (
    ALPHA_CLASS,
    CoreReactionFlowRatioAlpha,
    _InterArrivalRing,
    _MIN_OBSERVATIONS,
    _RECOMPUTE_INTERVAL,
    _RING_SIZE,
)


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert CoreReactionFlowRatioAlpha().manifest.alpha_id == "core_reaction_flow_ratio"

def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert CoreReactionFlowRatioAlpha().manifest.tier == AlphaTier.TIER_2

def test_manifest_paper_refs() -> None:
    assert "arXiv:2601.23172" in CoreReactionFlowRatioAlpha().manifest.paper_refs

def test_manifest_data_fields() -> None:
    f = CoreReactionFlowRatioAlpha().manifest.data_fields
    assert "timestamp_ns" in f and "price" in f

def test_manifest_latency_profile_set() -> None:
    assert CoreReactionFlowRatioAlpha().manifest.latency_profile is not None

def test_manifest_feature_set_version() -> None:
    assert CoreReactionFlowRatioAlpha().manifest.feature_set_version == "lob_shared_v1"

def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = CoreReactionFlowRatioAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS

def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is CoreReactionFlowRatioAlpha

def test_manifest_formula_uses_bacry() -> None:
    assert "sqrt(2/(m2+1))" in CoreReactionFlowRatioAlpha().manifest.formula


# --- Ring buffer ---
def test_ring_empty_zero() -> None:
    assert _InterArrivalRing(100).branching_ratio() == 0.0

def test_ring_insufficient_data_zero() -> None:
    ring = _InterArrivalRing(100)
    for i in range(_MIN_OBSERVATIONS - 1):
        ring.add(i * 1_000_000)
    assert ring.branching_ratio() == 0.0

def test_ring_regular_low_branching() -> None:
    ring = _InterArrivalRing(200)
    for i in range(100):
        ring.add(i * 1_000_000)
    assert ring.branching_ratio() == pytest.approx(0.0, abs=1e-6)

def test_ring_poisson_below_old_formula() -> None:
    rng = np.random.default_rng(42)
    ring = _InterArrivalRing(2000)
    ts = 0
    for dt in rng.exponential(1_000_000, size=1500):
        ts += int(dt) + 1
        ring.add(ts)
    assert ring.branching_ratio() < 0.25

def test_ring_clustered_high_branching() -> None:
    ring = _InterArrivalRing(500)
    ts = 0
    for i in range(200):
        ts += 100 if i % 6 < 5 else 100_000
        ring.add(ts)
    assert ring.branching_ratio() > 0.2

def test_ring_eviction() -> None:
    cap = 50
    ring = _InterArrivalRing(cap)
    for i in range(cap + 20):
        ring.add(i * 1_000_000)
    assert ring.count == cap
    assert ring.branching_ratio() == pytest.approx(0.0, abs=0.05)

def test_ring_reset() -> None:
    ring = _InterArrivalRing(100)
    for i in range(50):
        ring.add(i * 1_000_000)
    ring.reset()
    assert ring.count == 0

def test_ring_skips_zero_interval() -> None:
    ring = _InterArrivalRing(100)
    for i in range(50):
        ring.add(i * 1_000_000)
        ring.add(i * 1_000_000)
    assert ring.count == 49

def test_ring_skips_negative() -> None:
    ring = _InterArrivalRing(100)
    ring.add(100_000)
    ring.add(200_000)
    ring.add(150_000)
    assert ring.count == 1

def test_ring_recomputation() -> None:
    cap = 50
    ring = _InterArrivalRing(cap)
    for i in range(cap + _RECOMPUTE_INTERVAL + 100):
        ring.add(i * 1_000_000)
    assert ring.branching_ratio() == pytest.approx(0.0, abs=0.05)


# --- Signal ---
def test_first_tick_zero() -> None:
    assert CoreReactionFlowRatioAlpha().update(1_000_000, 100_0000, 10) == 0.0

def test_flat_price_zero() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    for i in range(100):
        alpha.update(i * 1_000_000, 100_0000, 10)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)

def test_symmetric_flow_near_zero() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price, ts = 100_0000, 0
    for i in range(200):
        ts += 1_000_000
        price += 1 if i % 2 == 0 else -1
        alpha.update(ts, price, 10)
    assert abs(alpha.get_signal()) < 0.5

def test_signal_clipped() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    rng = np.random.default_rng(42)
    price, ts = 100_0000, 0
    for _ in range(500):
        ts += int(rng.integers(1, 1_000_000))
        price += int(rng.choice([-10, -1, 0, 1, 10]))
        assert -2.0 <= alpha.update(ts, price, 10) <= 2.0


# --- Lee-Ready carry ---
def test_flat_ticks_carry_to_last_side() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    alpha.update(0, 100_0000, 10)
    alpha.update(1_000_000, 100_0001, 10)  # buy
    for i in range(5):
        alpha.update((2 + i) * 1_000_000, 100_0001, 10)  # flat -> carry buy
    assert alpha._buy_ring.count > 1
    assert alpha._sell_ring.count == 0

def test_flat_before_any_side_skipped() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    alpha.update(0, 100_0000, 10)
    for i in range(1, 10):
        alpha.update(i * 1_000_000, 100_0000, 10)
    assert alpha._buy_ring.count == 0
    assert alpha._sell_ring.count == 0


# --- Asymmetry detection ---
def test_clustered_buys_regular_sells_negative() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price, ts = 100_0000, 0
    for _ in range(20):
        for _ in range(10):
            ts += 100; price += 1
            alpha.update(ts, price, 10)
        for _ in range(10):
            ts += 1_000_000; price -= 1
            alpha.update(ts, price, 10)
    n_buy, n_sell = alpha.get_branching_ratios()
    assert n_buy > n_sell
    assert alpha.get_signal() < 0.0

def test_clustered_sells_regular_buys_positive() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price, ts = 100_0000, 0
    for _ in range(20):
        for _ in range(10):
            ts += 1_000_000; price += 1
            alpha.update(ts, price, 10)
        for _ in range(10):
            ts += 100; price -= 1
            alpha.update(ts, price, 10)
    n_buy, n_sell = alpha.get_branching_ratios()
    assert n_sell > n_buy
    assert alpha.get_signal() > 0.0

def test_asymmetry_nonzero() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price, ts = 100_0000, 0
    for _ in range(20):
        for _ in range(10):
            ts += 1_000_000; price += 1
            alpha.update(ts, price, 10)
        for _ in range(10):
            ts += 100; price -= 1
            alpha.update(ts, price, 10)
    assert alpha.get_asymmetry() > 0.01


# --- Warmup ---
def test_warmup_near_zero() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price, ts = 100_0000, 0
    for i in range(20):
        ts += 1_000_000
        price += 1 if i % 2 == 0 else -1
        assert abs(alpha.update(ts, price, 10)) < 0.1


# --- API ---
def test_keyword_args() -> None:
    assert isinstance(CoreReactionFlowRatioAlpha().update(timestamp_ns=1_000_000, price=100_0000, volume=10), float)

def test_two_positional() -> None:
    assert isinstance(CoreReactionFlowRatioAlpha().update(1_000_000, 100_0000), float)

def test_three_positional() -> None:
    assert isinstance(CoreReactionFlowRatioAlpha().update(1_000_000, 100_0000, 10), float)

def test_one_arg_raises() -> None:
    with pytest.raises(ValueError):
        CoreReactionFlowRatioAlpha().update(1_000_000)

def test_reset() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    for i in range(100):
        alpha.update(i * 1_000_000, 100_0000 + (i % 3 - 1), 10)
    alpha.reset()
    alpha2 = CoreReactionFlowRatioAlpha()
    assert alpha.update(1_000_000, 100_0000, 10) == pytest.approx(alpha2.update(1_000_000, 100_0000, 10), abs=1e-9)

def test_get_signal_before_update() -> None:
    assert CoreReactionFlowRatioAlpha().get_signal() == 0.0

def test_get_branching_ratios_tuple() -> None:
    n_buy, n_sell = CoreReactionFlowRatioAlpha().get_branching_ratios()
    assert n_buy == 0.0 and n_sell == 0.0


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(CoreReactionFlowRatioAlpha(), AlphaProtocol)


# --- Numerical ---
def test_uses_timestamps_not_depth() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    price = 100_0000
    for i in range(200):
        price += 1 if i % 2 == 0 else -1
        sig = alpha.update(i * 1_000_000, price, 1)
    assert math.isfinite(sig)

def test_large_timestamps() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    base = 1_711_100_000_000_000_000
    price = 100_0000
    for i in range(200):
        price += 1 if i % 2 == 0 else -1
        assert math.isfinite(alpha.update(base + i * 1_000_000, price, 10))

def test_monotonic_price_only_buys() -> None:
    alpha = CoreReactionFlowRatioAlpha()
    for i in range(200):
        alpha.update(i * 1_000_000, 100_0000 + i, 10)
    _, n_sell = alpha.get_branching_ratios()
    assert n_sell == 0.0

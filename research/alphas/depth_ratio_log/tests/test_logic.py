"""Gate B correctness tests for DepthRatioLogAlpha (ref 032)."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.depth_ratio_log.impl import (
    _A8,
    ALPHA_CLASS,
    DepthRatioLogAlpha,
)

# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert DepthRatioLogAlpha().manifest.alpha_id == "depth_ratio_log"


def test_manifest_hypothesis() -> None:
    m = DepthRatioLogAlpha().manifest
    assert "log" in m.hypothesis.lower()
    assert "depth" in m.hypothesis.lower() or "bid" in m.hypothesis.lower()


def test_manifest_formula() -> None:
    m = DepthRatioLogAlpha().manifest
    assert "EMA" in m.formula or "ema" in m.formula
    assert "log" in m.formula.lower()


def test_manifest_paper_refs_includes_032() -> None:
    assert "032" in DepthRatioLogAlpha().manifest.paper_refs


def test_manifest_complexity_o1() -> None:
    assert DepthRatioLogAlpha().manifest.complexity == "O(1)"


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert DepthRatioLogAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert DepthRatioLogAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS

    m = DepthRatioLogAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_manifest_data_fields() -> None:
    fields = DepthRatioLogAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier

    assert DepthRatioLogAlpha().manifest.tier == AlphaTier.TIER_2


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is DepthRatioLogAlpha


# ---------------------------------------------------------------------------
# Signal direction
# ---------------------------------------------------------------------------


def test_bid_dominance_gives_positive_signal() -> None:
    """Bid qty >> ask qty -> log ratio > 0 -> signal positive."""
    alpha = DepthRatioLogAlpha()
    for _ in range(20):
        alpha.update(500.0, 50.0)
    assert alpha.get_signal() > 0.5


def test_ask_dominance_gives_negative_signal() -> None:
    """Ask qty >> bid qty -> log ratio < 0 -> signal negative."""
    alpha = DepthRatioLogAlpha()
    for _ in range(20):
        alpha.update(50.0, 500.0)
    assert alpha.get_signal() < -0.5


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask -> log(1) = 0 -> signal = 0."""
    alpha = DepthRatioLogAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Signal bounds [-2, 2]
# ---------------------------------------------------------------------------


def test_signal_bounded_minus2_to_plus2_random() -> None:
    """Signal must stay in [-2, 2] for any random input pattern."""
    alpha = DepthRatioLogAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 300)
    asks = rng.uniform(0, 1000, 300)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -2.0 - 1e-9 <= sig <= 2.0 + 1e-9, f"Signal out of bounds: {sig}"


def test_signal_clipped_extreme_bid() -> None:
    """Extreme bid dominance should be clipped at +2."""
    alpha = DepthRatioLogAlpha()
    # log(1e10 / 1) = 23.0 >> 2
    sig = alpha.update(1e10, 0.5)
    assert sig == pytest.approx(2.0, abs=1e-9)


def test_signal_clipped_extreme_ask() -> None:
    """Extreme ask dominance should be clipped at -2."""
    alpha = DepthRatioLogAlpha()
    sig = alpha.update(0.5, 1e10)
    assert sig == pytest.approx(-2.0, abs=1e-9)


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_raw_log_ratio_constant_input() -> None:
    """EMA should converge to the raw log_ratio given constant input."""
    alpha = DepthRatioLogAlpha()
    bid, ask = 300.0, 100.0
    expected = math.log(300.0 / 100.0)
    for _ in range(200):
        alpha.update(bid, ask)
    assert alpha.get_signal() == pytest.approx(expected, abs=1e-4)


def test_ema_single_step_initializes_to_raw() -> None:
    """First update initializes EMA to the raw log_ratio (no prior history)."""
    alpha = DepthRatioLogAlpha()
    bid, ask = 400.0, 100.0
    expected = math.log(400.0 / 100.0)
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = DepthRatioLogAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    lr1 = math.log(300.0 / 100.0)
    lr2 = math.log(100.0 / 300.0)
    expected_ema2 = lr1 + _A8 * (lr2 - lr1)
    alpha.update(b1, a1)
    sig2 = alpha.update(b2, a2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = DepthRatioLogAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    expected = math.log(200.0 / 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = DepthRatioLogAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    expected = math.log(500.0 / 200.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_positional_args() -> None:
    alpha = DepthRatioLogAlpha()
    sig = alpha.update(300.0, 100.0)
    expected = math.log(300.0 / 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_get_signal_matches_last_update() -> None:
    alpha = DepthRatioLogAlpha()
    sig = alpha.update(200.0, 100.0)
    assert alpha.get_signal() == sig


def test_get_signal_before_update_is_zero() -> None:
    alpha = DepthRatioLogAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state() -> None:
    alpha = DepthRatioLogAlpha()
    alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, first update should equal raw log_ratio (no EMA history)
    bid, ask = 100.0, 100.0
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol

    alpha = DepthRatioLogAlpha()
    assert isinstance(alpha, AlphaProtocol)

"""Gate B correctness tests for QueueImbalanceAlpha (ref 125)."""
from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.queue_imbalance.impl import (
    ALPHA_CLASS,
    QueueImbalanceAlpha,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Manifest governance
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert QueueImbalanceAlpha().manifest.alpha_id == "queue_imbalance"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert QueueImbalanceAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs_includes_125() -> None:
    assert "125" in QueueImbalanceAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    fields = QueueImbalanceAlpha().manifest.data_fields
    assert "bid_qty" in fields
    assert "ask_qty" in fields


def test_manifest_latency_profile_set() -> None:
    """latency_profile must be non-None before Gate D (constitution requirement)."""
    assert QueueImbalanceAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert QueueImbalanceAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_manifest_valid_roles_and_skills() -> None:
    from research.registry.schemas import VALID_ROLES, VALID_SKILLS
    m = QueueImbalanceAlpha().manifest
    assert set(m.roles_used) <= VALID_ROLES
    assert set(m.skills_used) <= VALID_SKILLS


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is QueueImbalanceAlpha


# ---------------------------------------------------------------------------
# Signal boundary conditions
# ---------------------------------------------------------------------------


def test_equal_queues_signal_zero() -> None:
    """Equal bid and ask queues → QI = 0, signal = 0."""
    alpha = QueueImbalanceAlpha()
    sig = alpha.update(100.0, 100.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_bid_only_signal_positive() -> None:
    """Bid queue only → QI = 1 → signal positive."""
    alpha = QueueImbalanceAlpha()
    for _ in range(20):
        alpha.update(200.0, 0.0)
    assert alpha.get_signal() > 0.9


def test_ask_only_signal_negative() -> None:
    """Ask queue only → QI = -1 → signal negative."""
    alpha = QueueImbalanceAlpha()
    for _ in range(20):
        alpha.update(0.0, 200.0)
    assert alpha.get_signal() < -0.9


def test_signal_bounded_in_unit_interval() -> None:
    """Signal must stay in [-1, 1] at all times."""
    alpha = QueueImbalanceAlpha()
    rng = np.random.default_rng(42)
    bids = rng.uniform(0, 1000, 200)
    asks = rng.uniform(0, 1000, 200)
    for b, a in zip(bids, asks):
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0


# ---------------------------------------------------------------------------
# EMA convergence and correctness
# ---------------------------------------------------------------------------


def test_ema_converges_to_raw_qi_constant_input() -> None:
    """EMA should converge to the raw QI given constant input."""
    alpha = QueueImbalanceAlpha()
    bid, ask = 300.0, 100.0
    expected_qi = (bid - ask) / (bid + ask)
    for _ in range(100):
        alpha.update(bid, ask)
    assert alpha.get_signal() == pytest.approx(expected_qi, abs=1e-4)


def test_ema_single_step_initializes_to_raw_qi() -> None:
    """First update initializes EMA to the raw QI (no prior history)."""
    alpha = QueueImbalanceAlpha()
    bid, ask = 400.0, 100.0
    expected = (bid - ask) / (bid + ask)
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_ema_decay_second_step() -> None:
    """Second step: EMA = prev + alpha*(raw - prev)."""
    alpha = QueueImbalanceAlpha()
    b1, a1 = 300.0, 100.0
    b2, a2 = 100.0, 300.0
    qi1 = (b1 - a1) / (b1 + a1)
    qi2 = (b2 - a2) / (b2 + a2)
    expected_ema2 = qi1 + _EMA_ALPHA * (qi2 - qi1)
    alpha.update(b1, a1)
    sig2 = alpha.update(b2, a2)
    assert sig2 == pytest.approx(expected_ema2, abs=1e-9)


# ---------------------------------------------------------------------------
# API compatibility
# ---------------------------------------------------------------------------


def test_update_accepts_keyword_args() -> None:
    alpha = QueueImbalanceAlpha()
    sig = alpha.update(bid_qty=200.0, ask_qty=100.0)
    expected = (200.0 - 100.0) / (200.0 + 100.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_accepts_bids_asks_arrays() -> None:
    alpha = QueueImbalanceAlpha()
    bids = np.array([[100.0, 500.0], [99.0, 300.0]])
    asks = np.array([[101.0, 200.0], [102.0, 150.0]])
    sig = alpha.update(bids=bids, asks=asks)
    expected = (500.0 - 200.0) / (500.0 + 200.0)
    assert sig == pytest.approx(expected, abs=1e-9)


def test_update_one_arg_raises() -> None:
    alpha = QueueImbalanceAlpha()
    with pytest.raises(ValueError):
        alpha.update(100.0)


def test_reset_clears_state() -> None:
    alpha = QueueImbalanceAlpha()
    alpha.update(800.0, 100.0)
    alpha.reset()
    # After reset, first update should equal raw QI (no EMA history)
    bid, ask = 300.0, 300.0
    sig = alpha.update(bid, ask)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_get_signal_before_update_is_zero() -> None:
    alpha = QueueImbalanceAlpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# AlphaProtocol compliance
# ---------------------------------------------------------------------------


def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    alpha = QueueImbalanceAlpha()
    assert isinstance(alpha, AlphaProtocol)

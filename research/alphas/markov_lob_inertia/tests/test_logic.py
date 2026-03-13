"""Unit tests for markov_lob_inertia signal logic (>=15 tests).

Tests cover manifest integrity, Markov state transitions, EMA convergence,
inertia factor behaviour, signal bounds, state management, and API compat.
"""
from __future__ import annotations

import math

from research.alphas.markov_lob_inertia.impl import (
    MarkovLobInertiaAlpha,
    _DECAY,
    _EMA_ALPHA,
    _MANIFEST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alpha() -> MarkovLobInertiaAlpha:
    a = MarkovLobInertiaAlpha()
    a.reset()
    return a


def _warmup(alpha: MarkovLobInertiaAlpha, n: int = 200, bid: float = 100.0, ask: float = 100.0) -> None:
    for _ in range(n):
        alpha.update(bid, ask)


# ---------------------------------------------------------------------------
# 1-7: Manifest integrity
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert _MANIFEST.alpha_id == "markov_lob_inertia"


def test_manifest_paper_refs_contains_039() -> None:
    assert "039" in _MANIFEST.paper_refs


def test_manifest_data_fields() -> None:
    assert "bid_qty" in _MANIFEST.data_fields
    assert "ask_qty" in _MANIFEST.data_fields


def test_manifest_complexity() -> None:
    assert _MANIFEST.complexity == "O(1)"


def test_manifest_feature_set_version() -> None:
    assert _MANIFEST.feature_set_version == "lob_shared_v1"


def test_manifest_roles_used_non_empty() -> None:
    assert len(_MANIFEST.roles_used) > 0


def test_manifest_skills_used_non_empty() -> None:
    assert len(_MANIFEST.skills_used) > 0


# ---------------------------------------------------------------------------
# 8-9: Cold start and basic signal
# ---------------------------------------------------------------------------


def test_cold_start_does_not_raise() -> None:
    alpha = _make_alpha()
    result = alpha.update(100.0, 100.0)
    assert isinstance(result, float)
    assert math.isfinite(result)


def test_initial_signal_is_zero_before_update() -> None:
    alpha = _make_alpha()
    assert alpha.get_signal() == 0.0


# ---------------------------------------------------------------------------
# 10: First tick returns zero (no transition yet)
# ---------------------------------------------------------------------------


def test_first_tick_returns_zero() -> None:
    """First tick initializes prev_mid but cannot compute a transition."""
    alpha = _make_alpha()
    s = alpha.update(150.0, 100.0)
    assert s == 0.0


# ---------------------------------------------------------------------------
# 11-12: State classification and directional signal
# ---------------------------------------------------------------------------


def test_consistent_up_moves_give_positive_signal() -> None:
    """Repeatedly increasing mid_price -> UP state -> positive directional bias."""
    alpha = _make_alpha()
    for i in range(200):
        # Increasing mid_price via increasing bid dominance
        alpha.update(bid_qty=100.0 + i, ask_qty=50.0)
    assert alpha.get_signal() > 0.0


def test_consistent_down_moves_give_negative_signal() -> None:
    """Repeatedly decreasing mid_price -> DOWN state -> negative directional bias."""
    alpha = _make_alpha()
    for i in range(200):
        alpha.update(bid_qty=50.0, ask_qty=100.0 + i)
    assert alpha.get_signal() < 0.0


# ---------------------------------------------------------------------------
# 13: Balanced input -> signal near zero
# ---------------------------------------------------------------------------


def test_balanced_bid_ask_signal_near_zero() -> None:
    """Equal bid and ask queues -> FLAT state -> signal near zero."""
    alpha = _make_alpha()
    _warmup(alpha, 300)
    assert abs(alpha.get_signal()) < 0.1


# ---------------------------------------------------------------------------
# 14: EMA convergence with constant input
# ---------------------------------------------------------------------------


def test_ema_converges_with_constant_input() -> None:
    """After sufficient warmup with identical input, signal should stabilize."""
    alpha = _make_alpha()
    for _ in range(600):
        alpha.update(200.0, 100.0)
    s1 = alpha.update(200.0, 100.0)
    s2 = alpha.update(200.0, 100.0)
    assert abs(s2 - s1) < 1e-4, f"Signal did not converge: s1={s1}, s2={s2}"


# ---------------------------------------------------------------------------
# 15: Signal bounded to [-1, 1]
# ---------------------------------------------------------------------------


def test_signal_bounded_minus_1_to_plus_1() -> None:
    """Signal must stay within [-1.0, 1.0] for any input pattern."""
    import numpy as np

    alpha = _make_alpha()
    rng = np.random.default_rng(42)
    for _ in range(1000):
        bid = float(rng.uniform(0.0, 500.0))
        ask = float(rng.uniform(0.0, 500.0))
        s = alpha.update(bid, ask)
        assert -1.0 <= s <= 1.0, f"Signal out of bounds: {s}"


# ---------------------------------------------------------------------------
# 16: Reset clears all state
# ---------------------------------------------------------------------------


def test_reset_clears_all_state() -> None:
    alpha = _make_alpha()
    _warmup(alpha, 100, bid=200.0, ask=50.0)
    alpha.reset()
    assert alpha._signal == 0.0
    assert alpha._signal_ema == 0.0
    assert alpha._prev_mid == 0.0
    assert alpha._initialized is False


# ---------------------------------------------------------------------------
# 17: get_signal returns last update value
# ---------------------------------------------------------------------------


def test_get_signal_returns_last_update_value() -> None:
    alpha = _make_alpha()
    # Need two ticks: first initializes, second produces signal
    alpha.update(100.0, 100.0)
    result = alpha.update(150.0, 100.0)
    assert result == alpha.get_signal()


# ---------------------------------------------------------------------------
# 18: manifest property
# ---------------------------------------------------------------------------


def test_manifest_property_returns_correct_manifest() -> None:
    alpha = _make_alpha()
    assert alpha.manifest is _MANIFEST
    assert alpha.manifest.alpha_id == "markov_lob_inertia"


# ---------------------------------------------------------------------------
# 19-20: Positional and keyword API compatibility
# ---------------------------------------------------------------------------


def test_positional_api() -> None:
    alpha = _make_alpha()
    alpha.update(100.0, 100.0)  # init
    r = alpha.update(150.0, 100.0)
    assert math.isfinite(r)


def test_keyword_api_bid_ask_qty() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    # Init tick
    alpha1.update(100.0, 100.0)
    alpha2.update(bid_qty=100.0, ask_qty=100.0)
    # Second tick
    r1 = alpha1.update(150.0, 100.0)
    r2 = alpha2.update(bid_qty=150.0, ask_qty=100.0)
    assert abs(r1 - r2) < 1e-12


def test_positional_and_keyword_equivalent() -> None:
    """update(x, y) == update(bid_qty=x, ask_qty=y) for same state."""
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    inputs = [(100.0, 80.0), (120.0, 60.0), (90.0, 110.0)]
    for b, a in inputs:
        alpha1.update(b, a)
        alpha2.update(bid_qty=b, ask_qty=a)
    assert abs(alpha1.get_signal() - alpha2.get_signal()) < 1e-12


# ---------------------------------------------------------------------------
# 21: mid_price keyword overrides imbalance proxy
# ---------------------------------------------------------------------------


def test_mid_price_keyword_overrides_imbalance_proxy() -> None:
    """When mid_price is provided, it should be used instead of imbalance proxy."""
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
    # Same bid/ask but different explicit mid_price trajectories
    # Need enough ticks for transition matrix rows to diverge
    for i in range(50):
        alpha1.update(100.0, 100.0, mid_price=50.0 + i)  # UP trajectory
        alpha2.update(100.0, 100.0, mid_price=50.0 - i)  # DOWN trajectory
    # Different mid_price directions -> different signals
    assert alpha1.get_signal() != alpha2.get_signal()
    assert alpha1.get_signal() > 0.0
    assert alpha2.get_signal() < 0.0


# ---------------------------------------------------------------------------
# 22: Inertia factor (self-transition) increases with repetition
# ---------------------------------------------------------------------------


def test_inertia_increases_with_repeated_state() -> None:
    """Repeatedly visiting the same state increases self-transition probability."""
    alpha = _make_alpha()
    # Feed a series of increasing mid_prices -> UP state
    for i in range(100):
        alpha.update(100.0, 100.0, mid_price=float(100 + i))
    # UP->UP self-transition should dominate row 2 (UP)
    base = 2 * 3  # UP row
    t_up_up = alpha._transitions[base + 2]
    t_up_down = alpha._transitions[base + 0]
    assert t_up_up > t_up_down


# ---------------------------------------------------------------------------
# 23: EMA decay coefficient
# ---------------------------------------------------------------------------


def test_ema_alpha_coefficient() -> None:
    assert abs(_EMA_ALPHA - (1.0 - math.exp(-1.0 / 8.0))) < 1e-12


def test_decay_coefficient() -> None:
    assert abs(_DECAY - (1.0 - math.exp(-1.0 / 32.0))) < 1e-12

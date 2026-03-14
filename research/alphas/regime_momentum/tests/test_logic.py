"""Unit tests for regime_momentum signal logic (>=20 tests).

Tests cover manifest integrity, EMA convergence, regime factor bounds,
state management, signal direction, and API compatibility.
"""

from __future__ import annotations

import math

from research.alphas.regime_momentum.impl import (
    _A8,
    _A16,
    _A32,
    _A64,
    _MANIFEST,
    RegimeMomentumAlpha,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_alpha() -> RegimeMomentumAlpha:
    a = RegimeMomentumAlpha()
    a.reset()
    return a


def _warmup(alpha: RegimeMomentumAlpha, n: int = 200, bid: float = 100.0, ask: float = 100.0) -> None:
    for _ in range(n):
        alpha.update(bid, ask)


# ---------------------------------------------------------------------------
# 1-9: Manifest integrity
# ---------------------------------------------------------------------------


def test_manifest_alpha_id() -> None:
    assert _MANIFEST.alpha_id == "regime_momentum"


def test_manifest_paper_refs_contains_082() -> None:
    assert "082" in _MANIFEST.paper_refs


def test_manifest_latency_profile_not_none() -> None:
    assert _MANIFEST.latency_profile is not None
    assert "shioaji" in _MANIFEST.latency_profile


def test_manifest_feature_set_version() -> None:
    assert _MANIFEST.feature_set_version == "lob_shared_v1"


def test_manifest_data_fields() -> None:
    assert "bid_qty" in _MANIFEST.data_fields
    assert "ask_qty" in _MANIFEST.data_fields


def test_manifest_complexity() -> None:
    assert _MANIFEST.complexity == "O(1)"


def test_manifest_roles_used_non_empty() -> None:
    assert len(_MANIFEST.roles_used) > 0


def test_manifest_skills_used_non_empty() -> None:
    assert len(_MANIFEST.skills_used) > 0


def test_manifest_property_returns_correct_manifest() -> None:
    alpha = _make_alpha()
    assert alpha.manifest is _MANIFEST
    assert alpha.manifest.alpha_id == "regime_momentum"


# ---------------------------------------------------------------------------
# 10-12: Cold start and basic signal
# ---------------------------------------------------------------------------


def test_cold_start_does_not_raise() -> None:
    alpha = _make_alpha()
    result = alpha.update(100.0, 100.0)
    assert isinstance(result, float)
    assert math.isfinite(result)


def test_initial_signal_is_zero_before_update() -> None:
    alpha = _make_alpha()
    assert alpha.get_signal() == 0.0


def test_balanced_bid_ask_signal_near_zero() -> None:
    """Equal bid and ask queues -> OFI = 0 -> signal -> 0 over time."""
    alpha = _make_alpha()
    _warmup(alpha, 600)
    assert abs(alpha.get_signal()) < 0.05


# ---------------------------------------------------------------------------
# 13-14: EMA convergence
# ---------------------------------------------------------------------------


def test_ema_converges_with_constant_input() -> None:
    """After sufficient warmup ticks with identical input, EMA state should be stable.

    The slowest component is base64 (64-tick time constant -> ~5t = 320 ticks for
    99%+ convergence), so we warm up with 800 ticks.
    """
    alpha = _make_alpha()
    for _ in range(800):
        alpha.update(200.0, 100.0)
    s1 = alpha.update(200.0, 100.0)
    s2 = alpha.update(200.0, 100.0)
    assert abs(s2 - s1) < 1e-6, f"EMA did not converge: s1={s1}, s2={s2}"


def test_ema_alpha_coefficients() -> None:
    assert abs(_A8 - (1.0 - math.exp(-1.0 / 8.0))) < 1e-12
    assert abs(_A16 - (1.0 - math.exp(-1.0 / 16.0))) < 1e-12
    assert abs(_A32 - (1.0 - math.exp(-1.0 / 32.0))) < 1e-12
    assert abs(_A64 - (1.0 - math.exp(-1.0 / 64.0))) < 1e-12


# ---------------------------------------------------------------------------
# 15-17: Regime factor and momentum bounds
# ---------------------------------------------------------------------------


def test_signal_bounded_after_low_vol() -> None:
    """Long constant balanced input -> signal stays within [-2, 2]."""
    alpha = _make_alpha()
    _warmup(alpha, 500, bid=100.0, ask=100.0)
    assert abs(alpha.get_signal()) <= 2.0


def test_signal_bounded_after_high_vol() -> None:
    """Alternating extreme OFI -> signal stays within [-2, 2]."""
    alpha = _make_alpha()
    for i in range(300):
        if i % 2 == 0:
            alpha.update(200.0, 1.0)
        else:
            alpha.update(1.0, 200.0)
    assert abs(alpha.get_signal()) <= 2.0


def test_regime_factor_neutral_when_vol_matches_baseline() -> None:
    """If vol16 ~ base64 then rf ~ 1.0, and rf_momentum -> 0."""
    alpha = _make_alpha()
    _warmup(alpha, 800, bid=150.0, ask=100.0)
    # After long warmup rf settles, momentum should be near zero
    sig = abs(alpha.get_signal())
    assert sig < 0.1, f"Expected near-zero momentum, got {sig}"


# ---------------------------------------------------------------------------
# 18-19: Signal direction
# ---------------------------------------------------------------------------


def test_rising_vol_with_positive_ofi_gives_positive_signal() -> None:
    """Sudden vol increase with bid dominance -> positive signal during transition.

    regime_momentum captures transitions, not steady state. We check
    signal shortly after a vol regime shift (not after full convergence).
    """
    alpha = _make_alpha()
    # Warmup with balanced (low vol regime)
    _warmup(alpha, 300, bid=100.0, ask=100.0)
    # Inject strong bid dominance — vol spike, rf rises, rf_ema8 leads rf_ema32
    # Check signal during the first few ticks of the transition
    for _ in range(10):
        alpha.update(300.0, 50.0)
    # ofi_ema8 should be positive, rf_ema8 > rf_ema32 (fast reacts first)
    assert alpha.get_signal() > 0.0, f"Expected positive signal, got {alpha.get_signal()}"


def test_rising_vol_with_negative_ofi_gives_negative_signal() -> None:
    """Sudden vol increase with ask dominance -> negative signal during transition."""
    alpha = _make_alpha()
    _warmup(alpha, 300, bid=100.0, ask=100.0)
    for _ in range(10):
        alpha.update(50.0, 300.0)
    assert alpha.get_signal() < 0.0, f"Expected negative signal, got {alpha.get_signal()}"


# ---------------------------------------------------------------------------
# 20-22: State management
# ---------------------------------------------------------------------------


def test_reset_clears_all_state() -> None:
    alpha = _make_alpha()
    _warmup(alpha, 100, bid=200.0, ask=50.0)
    alpha.reset()
    assert alpha._ofi_ema8 == 0.0
    assert alpha._vol16 == 0.0
    assert alpha._base64 == 0.0
    assert alpha._rf_ema8 == 1.0
    assert alpha._rf_ema32 == 1.0
    assert alpha._signal == 0.0


def test_get_signal_returns_last_update_value() -> None:
    alpha = _make_alpha()
    result = alpha.update(150.0, 100.0)
    assert result == alpha.get_signal()


def test_get_signal_unchanged_between_updates() -> None:
    alpha = _make_alpha()
    alpha.update(150.0, 100.0)
    s1 = alpha.get_signal()
    s2 = alpha.get_signal()
    assert s1 == s2


# ---------------------------------------------------------------------------
# 23-25: Positional and keyword API compatibility
# ---------------------------------------------------------------------------


def test_positional_api() -> None:
    alpha = _make_alpha()
    r = alpha.update(150.0, 100.0)
    assert math.isfinite(r)


def test_keyword_api_bid_ask_qty() -> None:
    alpha1 = _make_alpha()
    alpha2 = _make_alpha()
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

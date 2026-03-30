"""Gate B correctness tests for LobKineticEnergyAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.lob_kinetic_energy.impl import (
    ALPHA_CLASS,
    LobKineticEnergyAlpha,
    _N_LEVELS,
    _SIGNAL_CLIP,
    _WARMUP_TICKS,
)


def _make_book(
    bid_qtys: list[float], ask_qtys: list[float]
) -> tuple[np.ndarray, np.ndarray]:
    bids = np.zeros((5, 2), dtype=np.float64)
    asks = np.zeros((5, 2), dtype=np.float64)
    for i, q in enumerate(bid_qtys):
        bids[i] = [100 - i, q]
    for i, q in enumerate(ask_qtys):
        asks[i] = [101 + i, q]
    return bids, asks


def _warmup(alpha: LobKineticEnergyAlpha, n: int = _WARMUP_TICKS + 5) -> None:
    bids, asks = _make_book([10] * 5, [10] * 5)
    for _ in range(n):
        alpha.update(bids=bids, asks=asks)


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert LobKineticEnergyAlpha().manifest.alpha_id == "lob_kinetic_energy"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert LobKineticEnergyAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    assert "arXiv:2308.14235" in LobKineticEnergyAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    f = LobKineticEnergyAlpha().manifest.data_fields
    assert "bid_px" in f and "ask_px" in f


def test_manifest_latency_profile_set() -> None:
    assert LobKineticEnergyAlpha().manifest.latency_profile is not None


def test_manifest_feature_set_version() -> None:
    assert LobKineticEnergyAlpha().manifest.feature_set_version == "lob_shared_v1"


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is LobKineticEnergyAlpha


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(LobKineticEnergyAlpha(), AlphaProtocol)


# --- Basic behavior ---
def test_first_tick_returns_zero() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [30, 20, 10, 5, 2])
    assert alpha.update(bids=bids, asks=asks) == 0.0


def test_get_signal_before_update() -> None:
    assert LobKineticEnergyAlpha().get_signal() == 0.0


def test_warmup_returns_zero() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([10] * 5, [10] * 5)
    for i in range(_WARMUP_TICKS - 1):
        sig = alpha.update(bids=bids, asks=asks)
        assert sig == 0.0, f"tick {i+1}: expected 0.0 during warmup, got {sig}"


# --- Signal direction ---
def test_bid_side_growth_positive_signal() -> None:
    """Growing bid quantities (buy pressure) should produce positive momentum."""
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    for i in range(1, 40):
        bid_qty = 10 + 5 * i  # growing bids
        bids, asks = _make_book([bid_qty] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() > 0.0, "bid growth should produce positive signal"


def test_ask_side_growth_negative_signal() -> None:
    """Growing ask quantities (sell pressure) should produce negative momentum."""
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    for i in range(1, 40):
        ask_qty = 10 + 5 * i  # growing asks
        bids, asks = _make_book([10] * 5, [ask_qty] * 5)
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() < 0.0, "ask growth should produce negative signal"


def test_symmetric_growth_near_zero() -> None:
    """Equal growth on both sides should produce near-zero momentum."""
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    for i in range(1, 80):
        qty = 10 + 3 * i
        bids, asks = _make_book([qty] * 5, [qty] * 5)
        alpha.update(bids=bids, asks=asks)
    assert abs(alpha.get_signal()) < 0.3, "symmetric growth should be near zero"


# --- Signal clipping ---
def test_signal_clipped_to_bounds() -> None:
    alpha = LobKineticEnergyAlpha()
    rng = np.random.default_rng(42)
    for _ in range(200):
        bids = np.column_stack(
            [np.arange(100, 95, -1, dtype=np.float64), rng.uniform(0, 10000, 5)]
        )
        asks = np.column_stack(
            [np.arange(101, 106, dtype=np.float64), rng.uniform(0, 10000, 5)]
        )
        sig = alpha.update(bids=bids, asks=asks)
        assert -_SIGNAL_CLIP <= sig <= _SIGNAL_CLIP


# --- Kinetic energy properties ---
def test_static_book_zero_energy() -> None:
    """A book that doesn't change should have zero kinetic energy."""
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(50):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_ke_bid() == pytest.approx(0.0, abs=1e-9)
    assert alpha.get_ke_ask() == pytest.approx(0.0, abs=1e-9)


def test_changing_book_nonzero_energy() -> None:
    """A book with changing quantities should have nonzero kinetic energy."""
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    bids, asks = _make_book([100, 80, 60, 40, 20], [10] * 5)  # big change
    alpha.update(bids=bids, asks=asks)
    assert alpha.get_ke_bid() > 0.0


def test_energy_ema_tracks_activity() -> None:
    """Energy EMA should be higher during active periods."""
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    # Quiet period
    bids, asks = _make_book([10] * 5, [10] * 5)
    for _ in range(30):
        alpha.update(bids=bids, asks=asks)
    energy_quiet = alpha.get_energy_ema()

    # Active period
    for i in range(1, 30):
        qty = 10 + 20 * i
        bids, asks = _make_book([qty] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    energy_active = alpha.get_energy_ema()

    assert energy_active > energy_quiet


# --- Active depth ---
def test_active_depth_1_ignores_deep_levels() -> None:
    """With active_depth=1, only L1 should matter."""
    alpha_1 = LobKineticEnergyAlpha(active_depth=1)
    alpha_5 = LobKineticEnergyAlpha(active_depth=5)

    bids_base, asks_base = _make_book([10] * 5, [10] * 5)
    alpha_1.update(bids=bids_base, asks=asks_base)
    alpha_5.update(bids=bids_base, asks=asks_base)

    # Change only deep levels (L3-L5)
    bids_deep, asks_deep = _make_book([10, 10, 100, 100, 100], [10] * 5)
    sig_1 = alpha_1.update(bids=bids_deep, asks=asks_deep)
    sig_5 = alpha_5.update(bids=bids_deep, asks=asks_deep)

    # active_depth=1 should not react to deep level changes
    # active_depth=5 should react
    assert abs(sig_1) < abs(sig_5) or sig_1 == pytest.approx(0.0, abs=1e-6)


# --- MLDM correlation (Challenger C1) ---
def test_mldm_correlation_insufficient_data() -> None:
    alpha = LobKineticEnergyAlpha()
    result = alpha.compute_mldm_correlation([0.5] * 5)
    assert result["sufficient_data"] is False


def test_mldm_correlation_with_data() -> None:
    alpha = LobKineticEnergyAlpha()
    _warmup(alpha)
    # Feed some varied data to build momentum history
    for i in range(1, 50):
        qty = 10 + 3 * i
        bids, asks = _make_book([qty, 10, 10, 10, 10], [10] * 5)
        alpha.update(bids=bids, asks=asks)

    mldm_fake = [float(i) * 0.1 for i in range(alpha._ring_count)]
    result = alpha.compute_mldm_correlation(mldm_fake)
    assert result["sufficient_data"] is True
    assert "correlation" in result
    assert "is_distinct" in result
    assert "theoretical_distinction" in result


# --- Boundary conditions ---
def test_requires_bids_asks_kwargs() -> None:
    with pytest.raises(ValueError, match="requires either"):
        LobKineticEnergyAlpha().update(50.0, 30.0)


def test_l1_flat_format_works() -> None:
    """L1 flat format (bid_px, ask_px, bid_qty, ask_qty) should work."""
    alpha = LobKineticEnergyAlpha()
    sig = alpha.update(bid_px=100_0000, ask_px=101_0000, bid_qty=50.0, ask_qty=30.0)
    assert isinstance(sig, float)
    sig2 = alpha.update(bid_px=100_0000, ask_px=101_0000, bid_qty=80.0, ask_qty=30.0)
    assert isinstance(sig2, float)


def test_fewer_than_5_levels_works() -> None:
    alpha = LobKineticEnergyAlpha()
    bids = np.array([[100, 50], [99, 40]], dtype=np.float64)
    asks = np.array([[101, 30]], dtype=np.float64)
    assert isinstance(alpha.update(bids=bids, asks=asks), float)


# --- Reset ---
def test_reset_clears_state() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([200, 100, 50, 25, 10], [30, 20, 10, 5, 2])
    alpha.update(bids=bids, asks=asks)
    alpha.reset()
    alpha2 = LobKineticEnergyAlpha()
    bids2, asks2 = _make_book([50] * 5, [50] * 5)
    assert alpha.update(bids=bids2, asks=asks2) == pytest.approx(
        alpha2.update(bids=bids2, asks=asks2), abs=1e-9
    )


# --- Numerical stability ---
def test_zero_depth_no_crash() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([0] * 5, [0] * 5)
    for _ in range(_WARMUP_TICKS + 10):
        sig = alpha.update(bids=bids, asks=asks)
    assert math.isfinite(sig)


def test_large_values_no_overflow() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([1e12] * 5, [1e12] * 5)
    for _ in range(_WARMUP_TICKS + 5):
        alpha.update(bids=bids, asks=asks)
    bids2, _ = _make_book([1e12 + 1e6] * 5, [1e12] * 5)
    assert math.isfinite(alpha.update(bids=bids2, asks=asks))


# --- EMA convergence ---
def test_ema_converges_to_zero() -> None:
    alpha = LobKineticEnergyAlpha()
    bids, asks = _make_book([50, 40, 30, 20, 10], [50, 40, 30, 20, 10])
    for _ in range(500):
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() == pytest.approx(0.0, abs=1e-6)


# --- Ring buffer (Allocator Law fix) ---
def test_ring_buffer_no_unbounded_growth() -> None:
    """History must use ring buffer, not unbounded list."""
    alpha = LobKineticEnergyAlpha(ring_size=64)
    _warmup(alpha)
    for i in range(1, 200):
        bids, asks = _make_book([10 + i] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    # Ring count should be capped at ring_size
    assert alpha._ring_count <= 64
    # Should not have list attributes
    assert not hasattr(alpha, "_ke_bid_history") or not isinstance(
        getattr(alpha, "_ke_bid_history", None), list
    )


def test_ring_buffer_wraps_correctly() -> None:
    """Ring buffer should wrap and not crash after exceeding size."""
    alpha = LobKineticEnergyAlpha(ring_size=16)
    for i in range(50):
        bids, asks = _make_book([10 + i] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    assert alpha._ring_count == 16  # capped
    assert alpha._ring_head == 49  # 50 writes minus 1 (first tick is init-only)
    assert math.isfinite(alpha.get_ke_bid())


def test_get_momentum_history_returns_recent() -> None:
    """_get_momentum_history should return most recent values."""
    alpha = LobKineticEnergyAlpha(ring_size=32)
    _warmup(alpha)
    for i in range(1, 20):
        bids, asks = _make_book([10 + 5 * i] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    hist = alpha._get_momentum_history(5)
    assert len(hist) == 5
    assert all(math.isfinite(v) for v in hist)


# --- skip_l1 (reduce OFI correlation) ---
def test_skip_l1_ignores_best_level() -> None:
    """With skip_l1=True, L1 changes should not affect signal."""
    alpha_skip = LobKineticEnergyAlpha(skip_l1=True)
    alpha_full = LobKineticEnergyAlpha(skip_l1=False)

    bids_base, asks_base = _make_book([10] * 5, [10] * 5)
    alpha_skip.update(bids=bids_base, asks=asks_base)
    alpha_full.update(bids=bids_base, asks=asks_base)

    # Change only L1
    bids_l1, asks_l1 = _make_book([100, 10, 10, 10, 10], [10] * 5)
    for _ in range(_WARMUP_TICKS + 5):
        alpha_skip.update(bids=bids_l1, asks=asks_l1)
        alpha_full.update(bids=bids_l1, asks=asks_l1)

    # skip_l1 should have zero or near-zero signal from L1-only change
    # full should have positive signal
    assert abs(alpha_skip.get_signal()) < abs(alpha_full.get_signal())


def test_skip_l1_reacts_to_deep_levels() -> None:
    """With skip_l1=True, deep level changes should still produce signal."""
    alpha = LobKineticEnergyAlpha(skip_l1=True)
    _warmup(alpha)
    for i in range(1, 40):
        deep_qty = 10 + 5 * i
        bids, asks = _make_book([10, deep_qty, deep_qty, deep_qty, deep_qty], [10] * 5)
        alpha.update(bids=bids, asks=asks)
    assert alpha.get_signal() > 0.0, "skip_l1 should still react to L2-L5"


# --- BBO-shift guard ---
def test_bbo_shift_zeroes_velocity() -> None:
    """When best price shifts, velocity should be zeroed to prevent spurious spikes."""
    alpha_guarded = LobKineticEnergyAlpha()

    # Initial book: bid@100, ask@101
    bids1 = np.array([[100, 50], [99, 40], [98, 30], [97, 20], [96, 10]], dtype=np.float64)
    asks1 = np.array([[101, 30], [102, 20], [103, 10], [104, 5], [105, 2]], dtype=np.float64)
    alpha_guarded.update(bids=bids1, asks=asks1)

    # BBO shifts: bid@101, ask@102 (prices shifted up by 1)
    # Quantities at new prices are different, but this is a price shift not a qty change
    bids2 = np.array([[101, 60], [100, 50], [99, 40], [98, 30], [97, 20]], dtype=np.float64)
    asks2 = np.array([[102, 25], [103, 15], [104, 8], [105, 3], [106, 1]], dtype=np.float64)

    # With BBO-shift guard, velocities should be zeroed for shifted levels
    alpha_guarded.update(bids=bids2, asks=asks2)
    # The KE should be much smaller than it would be without the guard
    # because the price shift zeroes out the velocity
    ke = alpha_guarded.get_ke_bid() + alpha_guarded.get_ke_ask()
    assert ke == pytest.approx(0.0, abs=1e-6), (
        f"BBO-shift guard should zero velocity when prices change, got KE={ke}"
    )


def test_bbo_stable_allows_velocity() -> None:
    """When prices are stable, velocity should be computed normally."""
    alpha = LobKineticEnergyAlpha()
    bids1 = np.array([[100, 50], [99, 40], [98, 30], [97, 20], [96, 10]], dtype=np.float64)
    asks1 = np.array([[101, 30], [102, 20], [103, 10], [104, 5], [105, 2]], dtype=np.float64)
    alpha.update(bids=bids1, asks=asks1)

    # Same prices, different quantities
    bids2 = np.array([[100, 80], [99, 60], [98, 40], [97, 25], [96, 15]], dtype=np.float64)
    asks2 = np.array([[101, 30], [102, 20], [103, 10], [104, 5], [105, 2]], dtype=np.float64)
    alpha.update(bids=bids2, asks=asks2)

    # Bid quantities increased, so bid KE should be nonzero
    assert alpha.get_ke_bid() > 0.0, "stable prices should allow velocity computation"


# --- Reset with new fields ---
def test_reset_clears_ring_buffer() -> None:
    alpha = LobKineticEnergyAlpha(ring_size=32)
    _warmup(alpha)
    for i in range(1, 20):
        bids, asks = _make_book([10 + i] * 5, [10] * 5)
        alpha.update(bids=bids, asks=asks)
    assert alpha._ring_count > 0
    alpha.reset()
    assert alpha._ring_count == 0
    assert alpha._ring_head == 0

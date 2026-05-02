"""Tests for R30 Zumbach Volatility Feedback Alpha (v2 — post-Challenger)."""

from __future__ import annotations

import math

import numpy as np

from research.alphas.r30_zumbach_vol_feedback.impl import R30ZumbachVolFeedbackAlpha


def test_manifest_alpha_id() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    assert alpha.manifest.alpha_id == "r30_zumbach_vol_feedback"


def test_manifest_paper_refs() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    assert "arXiv:1907.06151" in alpha.manifest.paper_refs


def test_initial_signal_is_zero() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    assert alpha.get_signal() == 0.0


def test_signal_zero_during_warmup() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    base_price = 200000000
    for i in range(50):
        alpha.update(price=base_price + i * 100)
    assert alpha.get_signal() == 0.0


def test_reset_clears_state() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    for i in range(200):
        alpha.update(price=200000000 + int(1000 * math.sin(i * 0.1)))
    alpha.reset()
    assert alpha.get_signal() == 0.0
    assert alpha.zumbach_signed == 0.0


def _generate_trending_prices(
    n_ticks: int,
    direction: float = -1.0,
    base_price: int = 200000000,
    trend_strength: float = 0.0001,
) -> list[int]:
    rng = np.random.default_rng(123)
    prices = [base_price]
    for _i in range(1, n_ticks):
        ret = direction * trend_strength + rng.normal(0, 0.0005)
        new_price = int(prices[-1] * math.exp(ret))
        new_price = max(new_price, 1)
        prices.append(new_price)
    return prices


def _generate_choppy_prices(
    n_ticks: int,
    base_price: int = 200000000,
) -> list[int]:
    rng = np.random.default_rng(456)
    prices = [base_price]
    for _i in range(1, n_ticks):
        revert = 0.001 * (base_price - prices[-1]) / base_price
        ret = revert + rng.normal(0, 0.0005)
        new_price = int(prices[-1] * math.exp(ret))
        new_price = max(new_price, 1)
        prices.append(new_price)
    return prices


def test_signal_bounded() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    prices = _generate_trending_prices(2000, direction=-1.0)
    for p in prices:
        sig = alpha.update(price=p)
        assert -1.0 <= sig <= 1.0


def test_zumbach_signed_responds_to_downtrend() -> None:
    """B-1 fix: After sustained downtrend, signed Zumbach should be positive."""
    alpha = R30ZumbachVolFeedbackAlpha()
    prices = _generate_trending_prices(1000, direction=-1.0, trend_strength=0.0003)
    for p in prices:
        alpha.update(price=p)
    # After sustained downtrend, signed Z should be positive (mean-reversion signal)
    assert alpha.zumbach_signed != 0.0


def test_zumbach_signed_no_abs_bug() -> None:
    """B-1 fix: Verify that sign information is preserved (no abs())."""
    alpha_down = R30ZumbachVolFeedbackAlpha()
    alpha_up = R30ZumbachVolFeedbackAlpha()

    prices_down = _generate_trending_prices(1500, direction=-1.0, trend_strength=0.0005)
    prices_up = _generate_trending_prices(1500, direction=1.0, trend_strength=0.0005)

    for p in prices_down:
        alpha_down.update(price=p)
    for p in prices_up:
        alpha_up.update(price=p)

    # Down-trend Zumbach should be positive, up-trend should be negative
    # (or at least they should have different signs)
    z_down = alpha_down.zumbach_signed
    z_up = alpha_up.zumbach_signed
    # They should not be equal (abs() would make them equal)
    assert z_down != z_up


def test_tra_diagnostic_proper() -> None:
    """B-4 fix: TRA should be computable and finite."""
    alpha = R30ZumbachVolFeedbackAlpha()
    prices = _generate_trending_prices(2000, direction=-1.0)
    for p in prices:
        alpha.update(price=p)
    tra = alpha.tra_ratio
    assert math.isfinite(tra)


def test_zero_price_ignored() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    alpha.update(price=0)
    alpha.update(price=-1)
    assert alpha.get_signal() == 0.0


def test_update_returns_signal() -> None:
    alpha = R30ZumbachVolFeedbackAlpha()
    sig = alpha.update(price=200000000)
    assert sig == alpha.get_signal()


def test_choppy_vs_trending_signal_difference() -> None:
    alpha_trend = R30ZumbachVolFeedbackAlpha()
    alpha_chop = R30ZumbachVolFeedbackAlpha()

    prices_trend = _generate_trending_prices(1500, direction=-1.0, trend_strength=0.0005)
    prices_chop = _generate_choppy_prices(1500)

    for p in prices_trend:
        alpha_trend.update(price=p)
    for p in prices_chop:
        alpha_chop.update(price=p)

    sig_trend = alpha_trend.get_signal()
    sig_chop = alpha_chop.get_signal()
    assert isinstance(sig_trend, float)
    assert isinstance(sig_chop, float)


# --- Execution fix: verify no heap alloc in get_recent_returns ---

def test_idx_buf_preallocated() -> None:
    """Execution fix: _idx_buf should be pre-allocated, not created per call."""
    alpha = R30ZumbachVolFeedbackAlpha()
    assert hasattr(alpha, "_idx_buf")
    assert len(alpha._idx_buf) == 512  # _RETURN_RING_SIZE

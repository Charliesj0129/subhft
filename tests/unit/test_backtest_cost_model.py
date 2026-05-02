"""tests/unit/test_backtest_cost_model.py

Unit tests for the asymmetric TAIFEX fee model in _compute_equity_curve.
"""

from __future__ import annotations

import numpy as np

from research.backtest.types import BacktestConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATA_PATH = "research/data/raw/dummy.npz"


def _make_config(**kwargs) -> BacktestConfig:
    defaults = dict(
        data_paths=[_BASE_DATA_PATH],
        taker_fee_bps=0.2,
        maker_fee_bps=-0.2,
        sell_tax_bps=2.0,
        initial_equity=1_000_000.0,
    )
    defaults.update(kwargs)
    return BacktestConfig(**defaults)


def _run_equity(prices, positions, config):
    """Import and call _compute_equity_curve directly."""
    from research.backtest.hft_native_runner import _compute_equity_curve

    return _compute_equity_curve(
        np.asarray(prices, dtype=np.float64),
        np.asarray(positions, dtype=np.float64),
        config,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_sell_tax_bps_zero_for_futures():
    """BacktestConfig default sell_tax_bps must be 0.0 (TAIFEX futures have no sell tax)."""
    cfg = BacktestConfig(data_paths=[_BASE_DATA_PATH])
    assert cfg.sell_tax_bps == 0.0


def test_sell_tax_applied_on_sells_only():
    """Sells should incur taker_fee + sell_tax; buys should incur taker_fee only.

    Scenario: compare the fee paid for a pure-buy step (position 0→1) vs a pure-
    sell step (position 1→0), both at the same notional price.  The sell step must
    cost more than the buy step by exactly sell_tax_bps × notional.
    """
    cfg = _make_config(taker_fee_bps=0.2, sell_tax_bps=2.0, initial_equity=0.0)
    price = 100.0

    # Pure buy: flat → long 1 at constant price (zero raw PnL)
    prices_buy = np.array([price, price], dtype=np.float64)
    positions_buy = np.array([0.0, 1.0], dtype=np.float64)
    eq_buy = _run_equity(prices_buy, positions_buy, cfg)
    cost_buy = -(eq_buy[-1] - eq_buy[0])  # cost is negative of equity change

    # Pure sell: long 1 → flat at constant price (zero raw PnL)
    prices_sell = np.array([price, price], dtype=np.float64)
    positions_sell = np.array([1.0, 0.0], dtype=np.float64)
    eq_sell = _run_equity(prices_sell, positions_sell, cfg)
    cost_sell = -(eq_sell[-1] - eq_sell[0])

    # Sell should cost more than buy by exactly sell_tax_bps on the notional
    assert cost_sell > cost_buy, (
        f"Sell cost {cost_sell:.8f} must exceed buy cost {cost_buy:.8f} "
        "because the sell leg carries the extra sell_tax_bps."
    )
    expected_extra = price * (2.0 / 10_000.0)
    assert abs((cost_sell - cost_buy) - expected_extra) < 1e-9, (
        f"Expected extra cost {expected_extra:.8f}, got {cost_sell - cost_buy:.8f}"
    )


def test_zero_sell_tax_matches_symmetric_model():
    """With sell_tax_bps=0, buy and sell fees should be equal (symmetric).

    The total fee paid for a long round-trip and a short round-trip at the
    same notional should be identical.
    """
    cfg = _make_config(taker_fee_bps=0.2, sell_tax_bps=0.0, initial_equity=0.0)
    price = 100.0

    prices = np.array([price, price, price], dtype=np.float64)
    positions_long = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    positions_short = np.array([0.0, -1.0, 0.0], dtype=np.float64)

    eq_long = _run_equity(prices, positions_long, cfg)
    eq_short = _run_equity(prices, positions_short, cfg)

    pnl_long = eq_long[-1] - eq_long[0]
    pnl_short = eq_short[-1] - eq_short[0]

    assert abs(pnl_long - pnl_short) < 1e-12, (
        f"With sell_tax_bps=0 both directions should have equal fees, but long={pnl_long:.8f} short={pnl_short:.8f}"
    )


def test_maker_fee_not_used_in_equity():
    """maker_fee_bps must NOT affect the equity curve (reserved for future use).

    Two configs that differ only in maker_fee_bps should produce identical
    equity curves, confirming the field is currently unused in computation.
    """
    cfg_positive_maker = _make_config(taker_fee_bps=0.2, maker_fee_bps=0.5, sell_tax_bps=2.0, initial_equity=0.0)
    cfg_negative_maker = _make_config(taker_fee_bps=0.2, maker_fee_bps=-0.5, sell_tax_bps=2.0, initial_equity=0.0)

    prices = np.array([100.0, 101.0, 100.0], dtype=np.float64)
    positions = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    eq_pos = _run_equity(prices, positions, cfg_positive_maker)
    eq_neg = _run_equity(prices, positions, cfg_negative_maker)

    np.testing.assert_array_almost_equal(
        eq_pos,
        eq_neg,
        decimal=12,
        err_msg="maker_fee_bps should not influence the equity curve (reserved field).",
    )


def test_default_taifex_fees_fee_magnitude():
    """With default TAIFEX fees a long round-trip at flat price should cost
    taker_fee on buy + (taker_fee + sell_tax) on sell.

    Round-trip cost = price * (buy_fee_rate + sell_fee_rate)
                    = price * ((0.2 + 0.2 + 2.0) / 10_000)
                    = price * 0.00024
    """
    cfg = _make_config(taker_fee_bps=0.2, sell_tax_bps=2.0, initial_equity=0.0)
    price = 10_000.0  # use round number for easy arithmetic

    prices = np.array([price, price, price], dtype=np.float64)
    positions = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    eq = _run_equity(prices, positions, cfg)
    total_cost = -(eq[-1] - eq[0])  # cost is negative PnL at flat price

    expected_buy_fee = price * (0.2 / 10_000.0)
    expected_sell_fee = price * ((0.2 + 2.0) / 10_000.0)
    expected_total = expected_buy_fee + expected_sell_fee

    assert abs(total_cost - expected_total) < 1e-9, (
        f"Expected total round-trip cost {expected_total:.8f}, got {total_cost:.8f}"
    )


def test_no_position_no_fee():
    """A flat-zero position series should incur zero fees and a flat equity curve."""
    cfg = _make_config(taker_fee_bps=0.5, sell_tax_bps=2.0, initial_equity=500_000.0)
    prices = np.array([100.0, 101.0, 102.0], dtype=np.float64)
    positions = np.array([0.0, 0.0, 0.0], dtype=np.float64)
    eq = _run_equity(prices, positions, cfg)

    # No trades → equity should stay at initial_equity throughout
    np.testing.assert_array_almost_equal(
        eq,
        np.full(3, 500_000.0),
        decimal=10,
        err_msg="Zero position should result in a flat equity curve at initial_equity.",
    )


def test_short_series_returns_base():
    """A price series shorter than 2 should return [initial_equity]."""
    from research.backtest.hft_native_runner import _compute_equity_curve

    cfg = _make_config(initial_equity=999_000.0)
    result = _compute_equity_curve(
        np.array([100.0]),
        np.array([1.0]),
        cfg,
    )
    assert result.shape == (1,)
    assert result[0] == 999_000.0

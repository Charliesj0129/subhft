"""Unit tests for PositionStore portfolio tracking: total_pnl, peak equity, and drawdown.

All prices/PnL values use scaled integers (x10000) — no floats in financial calculations.
Drawdown percentage is a display-only float, which is the expected and correct form.
"""

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.positions import PositionStore
from tests.factories import make_fill_event


def _make_fill(
    side: Side,
    qty: int,
    price: int,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "ACC",
    strategy_id: str = "STRAT",
    symbol: str = "SYM",
    ts: int = 0,
) -> FillEvent:
    """Delegate to shared factory with local defaults."""
    return make_fill_event(
        fill_id="fill-001",
        account_id=account_id,
        order_id="order-001",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=0,
        match_ts_ns=ts,
    )


@pytest.fixture()
def store():
    """PositionStore with metrics disabled for unit testing."""
    s = PositionStore()
    s.metrics = None  # avoid Prometheus side-effects in unit tests
    return s


# ---------------------------------------------------------------------------
# test_get_drawdown_pct_no_fills
# ---------------------------------------------------------------------------


def test_get_drawdown_pct_no_fills(store):
    """Empty store should return 0.0 — no peak, no drawdown."""
    assert store.get_drawdown_pct() == 0.0
    assert store.total_pnl == 0


# ---------------------------------------------------------------------------
# test_get_drawdown_pct_at_peak
# ---------------------------------------------------------------------------


def test_get_drawdown_pct_at_peak(store):
    """After a profit fill with no subsequent loss, drawdown must be 0.0."""
    # Buy 10 @ 100.0000 (1_000_000 scaled), sell 10 @ 100.5000 (1_005_000 scaled)
    # PnL = (1_005_000 - 1_000_000) * 10 = 50_000 scaled
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_005_000))

    assert store.total_pnl == 50_000
    assert store.get_drawdown_pct() == 0.0


# ---------------------------------------------------------------------------
# test_get_drawdown_pct_after_profit_then_loss
# ---------------------------------------------------------------------------


def test_get_drawdown_pct_after_profit_then_loss(store):
    """After profit then loss, drawdown fraction must be positive and <= 1.0."""
    # Round 1: Buy 10 @ 1_000_000, sell 10 @ 1_010_000 -> PnL = 100_000 (peak)
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000, symbol="SYM_A"))
    assert store.total_pnl == 100_000
    assert store.get_drawdown_pct() == 0.0  # still at peak

    # Round 2: Buy 10 @ 1_010_000, sell 10 @ 1_000_000 -> PnL = -100_000
    # Total PnL becomes 100_000 + (-100_000) = 0, peak stays at 100_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_000_000, symbol="SYM_A"))
    assert store.total_pnl == 0

    dd = store.get_drawdown_pct()
    # Drawdown = (100_000 - 0) / 100_000 = 1.0
    assert dd == pytest.approx(1.0, rel=1e-6)


def test_get_drawdown_pct_partial_drawdown(store):
    """Partial drawdown: lose half the peak equity."""
    # Build peak: PnL = 200_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_020_000))
    assert store.total_pnl == 200_000

    # Lose half: open and close at a 100_000 loss
    store.on_fill(_make_fill(Side.BUY, 10, 1_020_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000))
    assert store.total_pnl == 100_000

    dd = store.get_drawdown_pct()
    # (200_000 - 100_000) / 200_000 = 0.5
    assert dd == pytest.approx(0.5, rel=1e-6)


# ---------------------------------------------------------------------------
# test_total_pnl_aggregates_across_positions
# ---------------------------------------------------------------------------


def test_total_pnl_aggregates_across_positions(store):
    """total_pnl must be the sum of realized PnL across all symbol positions."""
    # SYM_A: buy 5 @ 1_000_000, sell 5 @ 1_010_000 -> PnL = 50_000
    store.on_fill(_make_fill(Side.BUY, 5, 1_000_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 5, 1_010_000, symbol="SYM_A"))

    # SYM_B: buy 10 @ 2_000_000, sell 10 @ 2_005_000 -> PnL = 50_000
    store.on_fill(_make_fill(Side.BUY, 10, 2_000_000, symbol="SYM_B"))
    store.on_fill(_make_fill(Side.SELL, 10, 2_005_000, symbol="SYM_B"))

    # SYM_C: buy 3 @ 500_000, sell 3 @ 490_000 -> PnL = -30_000
    store.on_fill(_make_fill(Side.BUY, 3, 500_000, symbol="SYM_C"))
    store.on_fill(_make_fill(Side.SELL, 3, 490_000, symbol="SYM_C"))

    expected_total = 50_000 + 50_000 + (-30_000)
    assert store.total_pnl == expected_total

    # Also verify individual position PnLs from Python cache
    pnl_a = store.positions["ACC:STRAT:SYM_A"].realized_pnl_scaled
    pnl_b = store.positions["ACC:STRAT:SYM_B"].realized_pnl_scaled
    pnl_c = store.positions["ACC:STRAT:SYM_C"].realized_pnl_scaled
    assert pnl_a + pnl_b + pnl_c == expected_total


# ---------------------------------------------------------------------------
# test_peak_equity_tracking
# ---------------------------------------------------------------------------


def test_peak_equity_tracking(store):
    """Peak equity must track the high watermark and never decrease."""
    # Step 1: first profit — peak = 100_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000))
    peak_after_step1 = store._peak_equity_scaled
    assert peak_after_step1 == 100_000

    # Step 2: loss — peak must stay at 100_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_005_000))
    assert store._peak_equity_scaled == peak_after_step1  # unchanged

    # Step 3: new profit that exceeds old peak — peak should update
    store.on_fill(_make_fill(Side.BUY, 10, 1_005_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_020_000))
    assert store._peak_equity_scaled > peak_after_step1


def test_peak_equity_never_negative_start(store):
    """If first trades are losing, peak stays 0 and drawdown returns 0.0."""
    # Buy @ 1_010_000 and sell @ 1_000_000: immediate loss
    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_000_000))

    assert store.total_pnl == -100_000
    # No positive peak was ever established
    assert store._peak_equity_scaled == 0
    # get_drawdown_pct returns 0.0 because peak <= 0 (system.py uses total_pnl fallback)
    assert store.get_drawdown_pct() == 0.0


# ---------------------------------------------------------------------------
# test_drawdown_resets_on_new_peak
# ---------------------------------------------------------------------------


def test_drawdown_resets_on_new_peak(store):
    """Drawdown should return to 0.0 when equity recovers to a new peak."""
    # Build peak: 100_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000))
    assert store.get_drawdown_pct() == 0.0

    # Drawdown: lose 50_000
    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_005_000))
    assert store.get_drawdown_pct() > 0.0

    # Recover and exceed old peak: gain 100_000 more
    store.on_fill(_make_fill(Side.BUY, 10, 1_005_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_015_000))
    # New total PnL = 100_000 - 50_000 + 100_000 = 150_000 > old peak 100_000
    assert store.get_drawdown_pct() == 0.0


# ---------------------------------------------------------------------------
# test_multiple_strategies_independent_positions
# ---------------------------------------------------------------------------


def test_multiple_strategies_independent_positions(store):
    """Fills for different strategy IDs must produce independent positions
    that all contribute to the portfolio total_pnl."""
    # Strategy A profits
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, strategy_id="ALPHA"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000, strategy_id="ALPHA"))

    # Strategy B loses
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, strategy_id="BETA"))
    store.on_fill(_make_fill(Side.SELL, 10, 990_000, strategy_id="BETA"))

    pnl_alpha = store.positions["ACC:ALPHA:SYM"].realized_pnl_scaled
    pnl_beta = store.positions["ACC:BETA:SYM"].realized_pnl_scaled

    assert pnl_alpha == 100_000
    assert pnl_beta == -100_000
    assert store.total_pnl == 0

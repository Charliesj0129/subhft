"""Verify Position.update() deducts fees from realized_pnl."""
from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import Side
from hft_platform.execution.positions import Position


def _make_fill(side: Side, price: int, qty: int, fee: int = 0, tax: int = 0) -> FillEvent:
    return FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=side, qty=qty, price=price,
        fee=fee, tax=tax, ingest_ts_ns=0, match_ts_ns=0,
    )


def test_realized_pnl_deducts_fees() -> None:
    """Fee deduction: fees subtracted on every fill, not just at close."""
    pos = Position(account_id="a1", strategy_id="s1", symbol="XMT")
    # Buy 1 @ 20000 (scaled: 200_000_000), commission 130_000
    pos.update(_make_fill(Side.BUY, 200_000_000, 1, fee=130_000, tax=0))
    # After buy: realized_pnl = 0 (no close) - 130_000 (fee) = -130_000
    assert pos.realized_pnl_scaled == -130_000

    # Sell 1 @ 20010 (scaled: 200_100_000), commission 130_000, tax 140_000
    pos.update(
        _make_fill(Side.SELL, 200_100_000, 1, fee=130_000, tax=140_000),
        contract_multiplier=10,
    )
    # Close PnL = (20010 - 20000) * 10 * 1 = 100 NTD = 1_000_000 scaled
    # Sell fees = 130_000 + 140_000 = 270_000
    # Total realized = -130_000 + (1_000_000 - 270_000) = 600_000
    assert pos.realized_pnl_scaled == 600_000
    assert pos.fees_scaled == 400_000


def test_gross_pnl_tracked_separately() -> None:
    pos = Position(account_id="a1", strategy_id="s1", symbol="XMT")
    pos.update(_make_fill(Side.BUY, 200_000_000, 1, fee=130_000))
    pos.update(
        _make_fill(Side.SELL, 200_100_000, 1, fee=130_000, tax=140_000),
        contract_multiplier=10,
    )
    # gross_pnl = 1_000_000 (the PnL without any fee deduction, close only)
    assert pos.gross_pnl_scaled == 1_000_000

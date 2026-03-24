"""Tests for shadow daily analysis logic."""

from __future__ import annotations


def test_compute_simulated_pnl_round_trip():
    """Buy then sell with 1-tick slippage."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders = [
        {"side": "BUY", "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
        {"side": "SELL", "mid_price": 201_0000, "qty": 1, "symbol": "TMF"},
    ]
    point_values = {"TMF": 10}
    pnl = compute_simulated_pnl(orders, point_values, slippage_ticks=1, tick_size=1)
    assert isinstance(pnl, int)
    # Buy at mid+1tick = 200_0001, sell at mid-1tick = 200_9999
    # PnL = (200_9999 - 200_0001) * 1 / 10000 * 10 = ~10 NTD minus 2 ticks slippage
    assert pnl > 0, f"Expected positive PnL for round trip with rising price, got {pnl}"


def test_compute_simulated_pnl_empty():
    from scripts.shadow_daily_report import compute_simulated_pnl

    assert compute_simulated_pnl([], {}, slippage_ticks=1, tick_size=1) == 0


def test_count_by_side():
    from scripts.shadow_daily_report import count_by_side

    orders = [{"side": "BUY"}, {"side": "BUY"}, {"side": "SELL"}]
    buys, sells = count_by_side(orders)
    assert buys == 2
    assert sells == 1


def test_count_by_side_empty():
    from scripts.shadow_daily_report import count_by_side

    assert count_by_side([]) == (0, 0)


def test_compute_simulated_pnl_loss():
    """Buy then sell below entry price yields a loss."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders = [
        {"side": "BUY", "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
        {"side": "SELL", "mid_price": 199_0000, "qty": 1, "symbol": "TMF"},
    ]
    point_values = {"TMF": 10}
    pnl = compute_simulated_pnl(orders, point_values, slippage_ticks=1, tick_size=1)
    assert isinstance(pnl, int)
    # Buy at 200_0001, sell at 198_9999
    # PnL = (198_9999 - 200_0001) * 1 / 10000 * 10 = negative
    assert pnl < 0, f"Expected negative PnL for round trip with falling price, got {pnl}"


def test_compute_simulated_pnl_mxf_point_value():
    """MXF uses 50 NTD/point vs TMF 10 NTD/point."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders_tmf = [
        {"side": "BUY", "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
        {"side": "SELL", "mid_price": 201_0000, "qty": 1, "symbol": "TMF"},
    ]
    orders_mxf = [
        {"side": "BUY", "mid_price": 200_0000, "qty": 1, "symbol": "MXF"},
        {"side": "SELL", "mid_price": 201_0000, "qty": 1, "symbol": "MXF"},
    ]
    point_values = {"TMF": 10, "MXF": 50}

    pnl_tmf = compute_simulated_pnl(orders_tmf, point_values, slippage_ticks=0, tick_size=0)
    pnl_mxf = compute_simulated_pnl(orders_mxf, point_values, slippage_ticks=0, tick_size=0)
    # MXF should be 5x the PnL of TMF for same price move
    assert pnl_mxf == 5 * pnl_tmf, f"MXF PnL {pnl_mxf} should be 5x TMF PnL {pnl_tmf}"


def test_compute_simulated_pnl_sell_without_open_position():
    """Orphan SELL with no matching BUY is skipped."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders = [
        {"side": "SELL", "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
    ]
    point_values = {"TMF": 10}
    pnl = compute_simulated_pnl(orders, point_values, slippage_ticks=1, tick_size=1)
    assert pnl == 0, f"Expected 0 PnL for orphan SELL, got {pnl}"


def test_compute_simulated_pnl_multiple_symbols():
    """PnL is computed independently per symbol."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders = [
        {"side": "BUY", "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
        {"side": "BUY", "mid_price": 10000_0000, "qty": 1, "symbol": "MXF"},
        {"side": "SELL", "mid_price": 201_0000, "qty": 1, "symbol": "TMF"},
        {"side": "SELL", "mid_price": 10001_0000, "qty": 1, "symbol": "MXF"},
    ]
    point_values = {"TMF": 10, "MXF": 50}
    pnl = compute_simulated_pnl(orders, point_values, slippage_ticks=0, tick_size=0)
    assert isinstance(pnl, int)
    assert pnl > 0, f"Expected positive combined PnL, got {pnl}"

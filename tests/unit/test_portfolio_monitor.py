"""Tests for PortfolioRiskMonitor (WU-10)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.portfolio_monitor import (
    PortfolioRiskMonitor,
    portfolio_concentration_ratio,
    portfolio_gross_exposure,
    portfolio_net_exposure,
    portfolio_open_positions,
    portfolio_unrealized_pnl,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    symbol: str,
    net_qty: int,
    avg_price_scaled: int,
    *,
    account_id: str = "acc1",
    strategy_id: str = "strat1",
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    pos.net_qty = net_qty
    pos.avg_price_scaled = avg_price_scaled
    pos.account_id = account_id
    pos.strategy_id = strategy_id
    return pos


def _make_store(positions: dict | None = None) -> MagicMock:
    store = MagicMock()
    store.positions = positions or {}
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_with_positions():
    """Two positions with known mid-prices produce correct gauges."""
    mid_prices = {"2330": 600_0000, "2317": 100_0000}
    store = _make_store(
        {
            "acc1:s:2330": _make_position("2330", 10, 590_0000),
            "acc1:s:2317": _make_position("2317", -5, 110_0000),
        }
    )

    monitor = PortfolioRiskMonitor(store, mid_price_cb=lambda s: mid_prices.get(s))
    monitor._snapshot()

    # gross = |10*6_000_000| + |-5*1_000_000| = 60_000_000 + 5_000_000
    assert portfolio_gross_exposure._value.get() == 65_000_000
    # net = 10*6_000_000 + (-5)*1_000_000 = 55_000_000
    assert portfolio_net_exposure._value.get() == 55_000_000
    assert portfolio_open_positions._value.get() == 2
    # concentration = 60_000_000 / 65_000_000
    assert portfolio_concentration_ratio._value.get() == pytest.approx(
        60_000_000 / 65_000_000, abs=1e-6
    )
    # unrealized: (600_0000-590_0000)*10 + (100_0000-110_0000)*(-5) = 100_0000 + 50_0000
    assert portfolio_unrealized_pnl._value.get() == 150_0000


@pytest.mark.asyncio
async def test_empty_portfolio():
    """Empty position store yields all-zero gauges."""
    store = _make_store()
    monitor = PortfolioRiskMonitor(store)
    monitor._snapshot()

    assert portfolio_gross_exposure._value.get() == 0
    assert portfolio_net_exposure._value.get() == 0
    assert portfolio_open_positions._value.get() == 0
    assert portfolio_concentration_ratio._value.get() == 0.0
    assert portfolio_unrealized_pnl._value.get() == 0


@pytest.mark.asyncio
async def test_concentration_ratio_single_position():
    """Single open position has concentration ratio = 1.0."""
    store = _make_store(
        {"acc1:s:2330": _make_position("2330", 5, 500_0000)}
    )
    monitor = PortfolioRiskMonitor(store, mid_price_cb=lambda _s: 510_0000)
    monitor._snapshot()

    assert portfolio_concentration_ratio._value.get() == pytest.approx(1.0)
    assert portfolio_open_positions._value.get() == 1


@pytest.mark.asyncio
async def test_missing_mid_prices_skipped():
    """Positions without mid-price are counted as open but not in exposure."""
    store = _make_store(
        {
            "acc1:s:2330": _make_position("2330", 10, 500_0000),
            "acc1:s:9999": _make_position("9999", 3, 100_0000),
        }
    )
    # Only 2330 has a mid-price
    monitor = PortfolioRiskMonitor(
        store, mid_price_cb=lambda s: 510_0000 if s == "2330" else None
    )
    monitor._snapshot()

    assert portfolio_open_positions._value.get() == 2
    # Only 2330 contributes to exposure: |10*510_0000| = 51_000_000
    assert portfolio_gross_exposure._value.get() == 51_000_000


@pytest.mark.asyncio
async def test_flat_positions_not_counted():
    """Positions with net_qty=0 are ignored."""
    store = _make_store(
        {"acc1:s:2330": _make_position("2330", 0, 500_0000)}
    )
    monitor = PortfolioRiskMonitor(store, mid_price_cb=lambda _s: 510_0000)
    monitor._snapshot()

    assert portfolio_open_positions._value.get() == 0
    assert portfolio_gross_exposure._value.get() == 0


@pytest.mark.asyncio
async def test_configurable_interval():
    """HFT_PORTFOLIO_MONITOR_INTERVAL_S env var changes the interval."""
    store = _make_store()
    with patch.dict("os.environ", {"HFT_PORTFOLIO_MONITOR_INTERVAL_S": "2"}):
        monitor = PortfolioRiskMonitor(store)
    assert monitor._interval_s == 2.0


@pytest.mark.asyncio
async def test_run_loop_publishes_and_stops():
    """run() publishes at least one snapshot and respects running flag."""
    store = _make_store(
        {"acc1:s:2330": _make_position("2330", 1, 100_0000)}
    )
    monitor = PortfolioRiskMonitor(store, mid_price_cb=lambda _s: 110_0000)

    with patch.dict("os.environ", {"HFT_PORTFOLIO_MONITOR_INTERVAL_S": "0.05"}):
        monitor = PortfolioRiskMonitor(store, mid_price_cb=lambda _s: 110_0000)

    async def _stop_after_delay():
        await asyncio.sleep(0.15)
        monitor.running = False

    await asyncio.gather(monitor.run(), _stop_after_delay())

    assert not monitor.running
    assert portfolio_open_positions._value.get() == 1


@pytest.mark.asyncio
async def test_no_mid_price_callback():
    """When mid_price_cb is None, exposure is zero but open count works."""
    store = _make_store(
        {"acc1:s:2330": _make_position("2330", 5, 500_0000)}
    )
    monitor = PortfolioRiskMonitor(store, mid_price_cb=None)
    monitor._snapshot()

    assert portfolio_open_positions._value.get() == 1
    assert portfolio_gross_exposure._value.get() == 0
    assert portfolio_unrealized_pnl._value.get() == 0

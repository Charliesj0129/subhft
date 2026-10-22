"""StormGuard's drawdown input is an absolute NTD give-back, not a ratio of the day's peak.

THESHOW 2026-10-05: a 4,620 NTD give-back read as 1.61% because the denominator was the
day's peak realised PnL, and set StormGuard to STORM; the same loss against a larger
peak would not have. An account that opens the day losing never armed the gate at all
(peak below ``_MIN_PEAK_SCALED``). Charlie chose an absolute NTD gate on 2026-10-11.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.positions import PositionStore
from hft_platform.risk.storm_guard import RiskThresholds
from hft_platform.services.system import HFTSystem

_SCALE = 10_000
_REF = 200_000


def _fill(side: Side, qty: int, price: int, symbol: str = "SYM") -> FillEvent:
    return FillEvent(
        fill_id="F1",
        account_id="ACC",
        order_id="O1",
        strategy_id="STRAT",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


@pytest.fixture()
def store() -> PositionStore:
    s = PositionStore()
    s.metrics = None
    return s


def _round_trip(store: PositionStore, pnl_ntd: int, symbol: str) -> None:
    """Realise ``pnl_ntd`` NTD in one buy/sell pair (qty 1, price delta = pnl_ntd)."""
    entry, exit_ = 1_000_000, 1_000_000 + pnl_ntd * _SCALE
    store.on_fill(_fill(Side.BUY, 1, entry, symbol))
    store.on_fill(_fill(Side.SELL, 1, exit_, symbol))


def test_a_loss_from_a_zero_start_counts_in_ntd_with_no_cold_start_guard(store):
    """The old ratio returned 0.0 here (peak 0 < _MIN_PEAK_SCALED): the gate never armed."""
    _round_trip(store, -2_500, "A")

    assert store.get_drawdown_pct() == 0.0, "the legacy ratio is blind to this loss"
    assert store.get_drawdown_ntd() == pytest.approx(2_500.0)


def test_the_same_giveback_reads_the_same_whatever_the_day_won_first(store):
    _round_trip(store, 20_000, "A")
    _round_trip(store, -3_000, "B")

    assert store.get_drawdown_ntd() == pytest.approx(3_000.0)


def test_no_drawdown_at_or_above_the_peak(store):
    assert store.get_drawdown_ntd() == 0.0
    _round_trip(store, 1_200, "A")
    assert store.get_drawdown_ntd() == 0.0


def test_the_ladder_over_the_default_reference_is_1000_2000_4000_ntd():
    thresholds = RiskThresholds()
    ntd = [
        abs(bps) / 10_000 * _REF
        for bps in (thresholds.warm_drawdown_bps, thresholds.storm_drawdown_bps, thresholds.halt_drawdown_bps)
    ]

    assert ntd == [1_000.0, 2_000.0, 4_000.0]


def test_the_supervisor_feeds_the_absolute_give_back_over_the_reference():
    store = MagicMock()
    store.get_drawdown_ntd.return_value = 2_000.0
    store.get_drawdown_pct.return_value = 0.9

    pct = HFTSystem._get_drawdown_pct(store, {})

    assert pct == pytest.approx(0.01)
    assert -int(pct * 10_000) == -100, "exactly the STORM line"


def test_a_configured_reference_replaces_the_default():
    store = MagicMock()
    store.get_drawdown_ntd.return_value = 1_000.0

    assert HFTSystem._get_drawdown_pct(store, {"drawdown_reference_ntd": 100_000}) == pytest.approx(0.01)


def test_reference_zero_restores_the_legacy_ratio():
    store = MagicMock()
    store.get_drawdown_ntd.return_value = 5_000.0
    store.get_drawdown_pct.return_value = 0.37

    assert HFTSystem._get_drawdown_pct(store, {"drawdown_reference_ntd": 0}) == 0.37


def test_a_garbage_reference_falls_back_to_the_default_not_to_zero():
    assert HFTSystem._drawdown_reference_ntd({"drawdown_reference_ntd": "abc"}) == 200_000
    assert HFTSystem._drawdown_reference_ntd({}) == 200_000


def test_a_store_without_the_ntd_reader_keeps_the_legacy_path():
    store = MagicMock(spec=["get_drawdown_pct"])
    store.get_drawdown_pct.return_value = 0.02

    assert HFTSystem._get_drawdown_pct(store, {}) == 0.02


def test_unrealised_loss_is_added_over_the_same_reference():
    pct = HFTSystem._combine_drawdown_with_mtm(
        realized_drawdown_pct=0.005, unrealized_scaled=-1_000 * _SCALE, base_capital=_REF
    )

    assert pct == pytest.approx(0.01), "0.5% realised + 1,000 / 200,000 unrealised"

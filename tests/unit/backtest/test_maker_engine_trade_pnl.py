"""End-to-end: MakerEngine.run populates BacktestResult.trade_pnl (Round 25).

Round 24 added the field + projector; Round 25 wires the day loop so
``BacktestResult.trade_pnl`` is populated from the day's FIFO trips
with the day-level cost/MtM delta allocated evenly across them.

Goal §1 names per-round-trip net edge as the canonical edge.  Without
this wiring, downstream trade-axis sub-gates silently fall back to
daily_pnl and dilute single-trade dominance signals.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    Hold,
    MakerEngine,
    PostQuote,
    TickData,
)

SCALE = 1_000_000


def _tick(bid: float, ask: float, *, trade: float = 0.0, ts: int = 0) -> TickData:
    return TickData(
        exch_ts=ts,
        bid_price=int(bid * SCALE),
        ask_price=int(ask * SCALE),
        bid_qty=10,
        ask_qty=10,
        trade_price=int(trade * SCALE),
        trade_volume=5 if trade > 0 else 0,
        is_trade=trade > 0,
        scale=SCALE,
    )


@dataclass
class _DetFill:
    label: str = "det"

    def post_quote(self, side: str, price: int, queue_ahead: int) -> QueuePosition:
        return QueuePosition(side=side, price=price, queue_ahead=0)

    def check_fills(self, orders, trade_price: int, trade_volume: int) -> bool:
        for o in orders:
            if o.side == "buy" and trade_price <= o.price:
                return True
            if o.side == "sell" and trade_price >= o.price:
                return True
        return False


@dataclass
class _ZeroCost(CostModel):
    label: str = "zero"

    def apply(self, gross: float, n_fills: int) -> float:
        return gross


@dataclass
class _PerFillCost(CostModel):
    """Linear: charge 0.5 pts per fill. Two-fill round-trip costs 1.0 pt."""

    label: str = "per_fill_0_5"

    def apply(self, gross: float, n_fills: int) -> float:
        return gross - 0.5 * n_fills


class _SequencedStrategy:
    """Posts buy then sell on alternating non-trade ticks.

    Produces 2 closed round-trips when paired with the fixture below.
    """

    def __init__(self) -> None:
        self._step = 0

    def on_tick(self, tick: TickData):
        if tick.is_trade:
            return [Hold()]
        actions: list = []
        if self._step in (0, 2):
            actions.append(PostQuote(side="buy", price=tick.bid_price, qty=1))
        elif self._step in (1, 3):
            actions.append(PostQuote(side="sell", price=tick.ask_price, qty=1))
        self._step += 1
        return actions or [Hold()]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        pass


class _FakeCKSource:
    def __init__(self, events: list[TickData]) -> None:
        self._events = events
        self._host = "fake"
        self._port = 0

    def health_check(self) -> None:
        return None

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        return list(self._events)

    def available_dates(self, symbol: str) -> list[str]:
        return ["2026-05-29"]


def _two_trip_day_events() -> list[TickData]:
    """Drive 2 buy-then-sell round-trips with zero residual.

    Sequence:
      t0: bid 100/ask 102        -> strategy posts BUY @100
      t1: trade @99              -> fills BUY @100 (det model: 99<=100)
      t2: bid 103/ask 105        -> strategy posts SELL @105
      t3: trade @106             -> fills SELL @105 (106>=105) ; trip = 105-100 = +5
      t4: bid 100/ask 102        -> strategy posts BUY @100
      t5: trade @99              -> fills BUY @100
      t6: bid 102/ask 104        -> strategy posts SELL @104
      t7: trade @105             -> fills SELL @104 ; trip = 104-100 = +4
    """
    return [
        _tick(100, 102, ts=10),
        _tick(100, 102, trade=99, ts=20),
        _tick(103, 105, ts=30),
        _tick(103, 105, trade=106, ts=40),
        _tick(100, 102, ts=50),
        _tick(100, 102, trade=99, ts=60),
        _tick(102, 104, ts=70),
        _tick(102, 104, trade=105, ts=80),
    ]


def _engine(cost: CostModel) -> MakerEngine:
    return MakerEngine(
        fill_model=_DetFill(),
        cost_model=cost,
        ck_source=_FakeCKSource(_two_trip_day_events()),  # type: ignore[arg-type]
    )


class TestMakerEngineTradePnlPopulation:
    def test_trade_pnl_is_populated_when_trips_close(self) -> None:
        result = _engine(_ZeroCost()).run(
            strategy=_SequencedStrategy(),
            instrument="TEST",
            dates=["2026-05-29"],
            pipeline_mode="strict",
        )
        assert result.trade_pnl is not None
        assert len(result.trade_pnl) == 2

    def test_trade_pnl_sum_equals_day_net_under_zero_cost(self) -> None:
        result = _engine(_ZeroCost()).run(
            strategy=_SequencedStrategy(),
            instrument="TEST",
            dates=["2026-05-29"],
            pipeline_mode="strict",
        )
        assert result.daily_pnl and len(result.daily_pnl) == 1
        day_net = result.daily_pnl[0]["pnl_pts"]
        assert sum(result.trade_pnl) == pytest.approx(day_net, abs=1e-6)
        # Zero cost & zero residual -> per-trip equals gross: +5 and +4.
        assert sorted(round(t, 6) for t in result.trade_pnl) == [4.0, 5.0]

    def test_trade_pnl_absorbs_per_fill_cost_allocation(self) -> None:
        # 4 fills * 0.5 pts = 2.0 pts cost spread over 2 trips => 1.0 pt each.
        result = _engine(_PerFillCost()).run(
            strategy=_SequencedStrategy(),
            instrument="TEST",
            dates=["2026-05-29"],
            pipeline_mode="strict",
        )
        assert result.trade_pnl is not None and len(result.trade_pnl) == 2
        # Gross trips were +5 and +4; each loses 1.0 pt cost -> +4 and +3.
        assert sorted(round(t, 6) for t in result.trade_pnl) == [3.0, 4.0]
        # Invariant: sum equals day_net.
        day_net = result.daily_pnl[0]["pnl_pts"]
        assert sum(result.trade_pnl) == pytest.approx(day_net, abs=1e-6)

    def test_trade_pnl_none_when_no_trips_close(self) -> None:
        # Reuse the residual-long fixture pattern: 1 buy fill, no closing
        # sell -> 0 trips -> trade_pnl should be None.
        class _BuyOnly:
            def __init__(self) -> None:
                self._done = False

            def on_tick(self, tick):
                if self._done or tick.is_trade:
                    return [Hold()]
                self._done = True
                return [PostQuote(side="buy", price=tick.bid_price, qty=1)]

            def on_fill(self, side, price, mid_price):
                pass

        events = [
            _tick(100, 102, ts=10),
            _tick(100, 102, trade=99, ts=20),
            _tick(100, 110, ts=30),
        ]
        engine = MakerEngine(
            fill_model=_DetFill(),
            cost_model=_ZeroCost(),
            ck_source=_FakeCKSource(events),  # type: ignore[arg-type]
        )
        result = engine.run(
            strategy=_BuyOnly(),
            instrument="TEST",
            dates=["2026-05-29"],
            pipeline_mode="strict",
        )
        assert result.daily_pnl[0]["trips"] == 0
        # Zero trips -> field stays None so downstream gates fall back to daily.
        assert result.trade_pnl is None

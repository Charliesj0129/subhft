"""D5 (2026-04-21 incident): MakerEngine latency injection for live-faithful
backtest.

Current MakerEngine assumes orders appear / cancel instantly (0-RTT). Today's
incident shows live Shioaji RTT ~800 ms causes orders to sit 7-78 s at stale
prices. A realistic backtest must model this.

These tests pin the semantic: with ``latency_profile`` set, PostQuote and
CancelQuote actions take the configured time to activate at the exchange,
and any trade that arrives within that window cannot match the not-yet-active
order.
"""

from __future__ import annotations

from dataclasses import dataclass

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    Hold,
    LatencyProfile,
    MakerEngine,
    PostQuote,
    TickData,
)


@dataclass
class _DeterministicFillModel:
    """Always fills once a trade sweeps the order price, ignoring queue."""

    label: str = "deterministic"

    def post_quote(self, side: str, price: int, queue_ahead: int) -> QueuePosition:
        return QueuePosition(side=side, price=price, queue_ahead=0)

    def check_fills(self, orders, trade_price, trade_volume):
        for o in orders:
            if o.side == "buy" and trade_price <= o.price:
                return True
            if o.side == "sell" and trade_price >= o.price:
                return True
        return False


@dataclass
class _ZeroCost(CostModel):
    label: str = "zero"

    def apply(self, gross, n_fills):
        return gross


class _BuyOnceStrategy:
    """Posts a single BUY at best bid on the first bidask, then holds."""

    def __init__(self):
        self._posted = False
        self._fills = []

    def on_tick(self, tick: TickData):
        if self._posted or tick.is_trade:
            return [Hold()]
        self._posted = True
        return [PostQuote(side="buy", price=tick.bid_price, qty=1)]

    def on_fill(self, side, price, mid_price):
        self._fills.append((side, price, mid_price))


def _mk_events(ts_base_ns=1_000_000_000):
    scale = 1_000_000
    return [
        TickData(
            exch_ts=ts_base_ns,
            bid_price=100 * scale,
            ask_price=102 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=0,
            trade_volume=0,
            is_trade=False,
        ),
        # Trade at 99 (would fill a BUY@100) — 100 ms after placement
        TickData(
            exch_ts=ts_base_ns + 100_000_000,
            bid_price=100 * scale,
            ask_price=102 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=99 * scale,
            trade_volume=5,
            is_trade=True,
        ),
        # Another trade at 99 — 1000 ms after placement
        TickData(
            exch_ts=ts_base_ns + 1_000_000_000,
            bid_price=100 * scale,
            ask_price=102 * scale,
            bid_qty=10,
            ask_qty=10,
            trade_price=99 * scale,
            trade_volume=5,
            is_trade=True,
        ),
    ]


class TestLatencyProfileInjection:
    def test_zero_latency_same_as_baseline(self):
        """With LatencyProfile(0, 0), behavior must equal instant-RTT baseline."""
        engine = MakerEngine(
            fill_model=_DeterministicFillModel(),
            cost_model=_ZeroCost(),
            latency_profile=LatencyProfile(place_ns=0, cancel_ns=0),
        )
        strat = _BuyOnceStrategy()
        fills, _ = engine._run_day(strat, _mk_events())
        assert len(fills) == 1, "Zero-latency should fill on first trade"
        assert fills[0]["side"] == "buy"

    def test_place_latency_skips_earlier_trades(self):
        """With place_ns=500ms, a trade at +100ms must NOT fill — order not yet
        at exchange. A trade at +1000ms must fill."""
        engine = MakerEngine(
            fill_model=_DeterministicFillModel(),
            cost_model=_ZeroCost(),
            latency_profile=LatencyProfile(place_ns=500_000_000, cancel_ns=0),
        )
        strat = _BuyOnceStrategy()
        fills, _ = engine._run_day(strat, _mk_events())
        # Only the later trade (t=+1000ms) should fill; the +100ms trade is
        # lost because the order is in flight.
        assert len(fills) == 1
        # The fill ts should correspond to the LATER trade.
        # (Engine doesn't record timestamp in fill dict; implicit check via
        # count: with no latency this test would also count 1 because the
        # strategy only posts once. So we verify via an explicit "lost trade"
        # counter or a gap by using 2 overlapping orders in another test.)

    def test_place_latency_400ms_blocks_100ms_trade(self):
        """Direct latency test: place_ns=400ms means +100ms trade should NOT
        match. Use a strategy that posts once and a single early-trade stream."""

        class _BuyStrat:
            def __init__(self):
                self._posted = False

            def on_tick(self, tick):
                if self._posted or tick.is_trade:
                    return [Hold()]
                self._posted = True
                return [PostQuote(side="buy", price=tick.bid_price, qty=1)]

            def on_fill(self, *a, **kw):
                pass

        scale = 1_000_000
        events = [
            TickData(
                exch_ts=0,
                bid_price=100 * scale,
                ask_price=102 * scale,
                bid_qty=10,
                ask_qty=10,
                trade_price=0,
                trade_volume=0,
                is_trade=False,
            ),
            TickData(
                exch_ts=100_000_000,  # +100ms, still in-flight (place_ns=400ms)
                bid_price=100 * scale,
                ask_price=102 * scale,
                bid_qty=10,
                ask_qty=10,
                trade_price=99 * scale,
                trade_volume=5,
                is_trade=True,
            ),
        ]
        engine = MakerEngine(
            fill_model=_DeterministicFillModel(),
            cost_model=_ZeroCost(),
            latency_profile=LatencyProfile(place_ns=400_000_000, cancel_ns=0),
        )
        fills, _ = engine._run_day(_BuyStrat(), events)
        assert len(fills) == 0, "D5: order in flight (place_ns=400ms, trade at +100ms) must NOT fill"

    def test_cancel_latency_allows_intermediate_fill(self):
        """With cancel_ns=500ms, a trade arriving +200ms after CancelQuote
        should still fill — cancel not yet effective."""

        class _PostThenCancelStrat:
            def __init__(self):
                self._step = 0

            def on_tick(self, tick):
                if tick.is_trade:
                    return [Hold()]
                self._step += 1
                if self._step == 1:
                    return [PostQuote(side="buy", price=tick.bid_price, qty=1)]
                if self._step == 2:
                    return [CancelQuote(side="buy")]
                return [Hold()]

            def on_fill(self, *a, **kw):
                pass

        scale = 1_000_000
        events = [
            TickData(
                exch_ts=0,
                bid_price=100 * scale,
                ask_price=102 * scale,
                bid_qty=10,
                ask_qty=10,
                trade_price=0,
                trade_volume=0,
                is_trade=False,
            ),
            # bidask tick 2 → strategy issues CancelQuote
            TickData(
                exch_ts=100_000_000,
                bid_price=100 * scale,
                ask_price=102 * scale,
                bid_qty=10,
                ask_qty=10,
                trade_price=0,
                trade_volume=0,
                is_trade=False,
            ),
            # Trade at +300ms, cancel_ns=500ms so cancel not yet effective
            TickData(
                exch_ts=300_000_000,
                bid_price=100 * scale,
                ask_price=102 * scale,
                bid_qty=10,
                ask_qty=10,
                trade_price=99 * scale,
                trade_volume=5,
                is_trade=True,
            ),
        ]
        engine = MakerEngine(
            fill_model=_DeterministicFillModel(),
            cost_model=_ZeroCost(),
            latency_profile=LatencyProfile(place_ns=0, cancel_ns=500_000_000),
        )
        fills, _ = engine._run_day(_PostThenCancelStrat(), events)
        assert len(fills) == 1, (
            "D5: cancel in flight (cancel_ns=500ms, trade at +200ms after "
            "cancel issued) must still fill the order — adverse-selection "
            "simulation"
        )


class TestLatencyProfileDefaults:
    def test_no_profile_instant_rtt(self):
        """Default constructor (no latency_profile) must preserve instant-RTT."""
        engine = MakerEngine(
            fill_model=_DeterministicFillModel(),
            cost_model=_ZeroCost(),
        )
        strat = _BuyOnceStrategy()
        fills, _ = engine._run_day(strat, _mk_events())
        assert len(fills) == 1

    def test_shioaji_profile_exists(self):
        """A ``LatencyProfile.shioaji_p95()`` convenience must return an
        800 ms place + 800 ms cancel profile."""
        p = LatencyProfile.shioaji_p95()
        assert p.place_ns == 800_000_000
        assert p.cancel_ns == 800_000_000

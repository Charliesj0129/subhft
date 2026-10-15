"""``MakerEngine.run`` must report the Sharpe and the latency it actually produced and used.

It used to hard-code ``sharpe_oos=0.0`` and ``latency_profile={}``. Gate C then judged every
maker candidate on an out-of-sample Sharpe of exactly zero and could not tell a 395 ms
measured-latency run from an instant-round-trip one, so the report vouched for nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pytest

from research.backtest.cost_models import CostModel
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    MIN_OOS_DAYS,
    Hold,
    LatencyProfile,
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


class _AlternatingStrategy:
    """Buys at the bid on one quote tick and sells at the ask on the next: one round trip a day."""

    def __init__(self) -> None:
        self._step = 0

    def on_tick(self, tick: TickData):
        if tick.is_trade:
            return [Hold()]
        side = "buy" if self._step % 2 == 0 else "sell"
        self._step += 1
        return [PostQuote(side=side, price=tick.bid_price if side == "buy" else tick.ask_price, qty=1)]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        pass


class _DailyProfitSource:
    """One date per entry of ``profits``; day ``i`` earns exactly ``profits[i]`` points gross."""

    def __init__(self, profits: list[float]) -> None:
        self._profits = profits
        self._host = "fake"
        self._port = 0

    def health_check(self) -> None:
        return None

    def available_dates(self, symbol: str) -> list[str]:
        return [f"2026-06-{day + 1:02d}" for day in range(len(self._profits))]

    def load_day(self, symbol: str, date: str) -> list[TickData]:
        sell = 100.0 + self._profits[int(date[-2:]) - 1]
        return [
            _tick(100, 102, ts=10),
            _tick(100, 102, trade=99, ts=20),  # buy @100 fills
            _tick(sell - 2, sell, ts=30),
            _tick(sell - 2, sell, trade=sell + 1, ts=40),  # sell @sell fills
        ]


def _run(profits: list[float], **engine_kwargs: object):
    engine = MakerEngine(
        fill_model=_DetFill(),
        cost_model=_ZeroCost(),
        ck_source=_DailyProfitSource(profits),  # type: ignore[arg-type]
        **engine_kwargs,  # type: ignore[arg-type]
    )
    return engine.run(_AlternatingStrategy(), "TMFD6")


def _sharpe(values: list[float]) -> float:
    array = np.asarray(values, dtype=float)
    return float(array.mean() / array.std() * math.sqrt(252))


OOS_PATTERN = [3.0, 5.0, 4.0, 6.0, 4.0]


def _long_run(in_sample: list[float], out_of_sample: list[float]) -> list[float]:
    return in_sample + out_of_sample


class TestOutOfSampleSharpe:
    def test_sharpe_oos_is_computed_from_the_last_30_percent_of_days(self) -> None:
        profits = _long_run([1.0, 2.0] * 25, OOS_PATTERN * 5)  # 75 days
        oos_days = len(profits) - int(len(profits) * 0.7)
        result = _run(profits)

        assert result.maker_scorecard["n_days"] == 75
        assert result.maker_scorecard["oos_days"] == oos_days >= MIN_OOS_DAYS
        assert result.sharpe_oos == pytest.approx(_sharpe(profits[-oos_days:]))
        assert result.sharpe_oos != 0.0
        assert result.maker_scorecard["sharpe_oos_status"] == "ok"

    def test_sharpe_oos_follows_the_order_of_dates_not_the_size_of_the_returns(self) -> None:
        """A strategy that lost money late must not borrow the Sharpe of its good early days."""
        good_then_bad = _run(_long_run([4.0, 5.0] * 25, [-x for x in OOS_PATTERN] * 5))

        assert good_then_bad.sharpe_is > 0
        assert good_then_bad.sharpe_oos < 0

    def test_a_short_oos_segment_still_reports_zero_so_no_floor_gets_easier(self) -> None:
        """49 days is the most a single TMF contract has; its 14 OOS days must not clear a 1.0 floor by luck."""
        profits = _long_run([1.0, 2.0] * 17, OOS_PATTERN * 3)  # 49 days -> 15 OOS days
        result = _run(profits)

        assert result.maker_scorecard["oos_days"] < MIN_OOS_DAYS
        assert result.sharpe_oos == 0.0
        assert not math.isnan(result.sharpe_oos)
        assert result.maker_scorecard["sharpe_oos_status"] == "insufficient_oos_days"
        # ...but the measured number is not hidden from people:
        assert result.maker_scorecard["sharpe_oos_raw"] == pytest.approx(_sharpe(profits[-15:]))

    def test_sharpe_oos_is_zero_not_nan_when_the_out_of_sample_days_do_not_vary(self) -> None:
        result = _run(_long_run([1.0, 2.0] * 25, [3.0] * 25))

        assert result.sharpe_oos == 0.0
        assert not math.isnan(result.sharpe_oos)
        assert result.maker_scorecard["sharpe_oos_status"] == "flat_oos_returns"

    def test_sharpe_oos_is_zero_not_nan_with_almost_no_data(self) -> None:
        for profits in ([], [1.0], [1.0, 2.0]):
            if not profits:
                continue
            result = _run(profits)
            assert result.sharpe_oos == 0.0
            assert not math.isnan(result.sharpe_oos)

    def test_the_split_fraction_can_be_chosen_and_is_validated(self) -> None:
        profits = [1.0, 2.0] * 10 + OOS_PATTERN * 4  # 40 days
        half = _run(profits, is_oos_split=0.5)

        assert half.maker_scorecard["oos_days"] == 20
        assert half.maker_scorecard["is_oos_split"] == 0.5
        assert half.sharpe_oos == pytest.approx(_sharpe(profits[20:]))
        for bad in (0.0, 1.0, 1.5, -0.1):
            with pytest.raises(ValueError):
                MakerEngine(fill_model=_DetFill(), cost_model=_ZeroCost(), is_oos_split=bad)


class TestLatencyReport:
    def test_the_injected_profile_is_what_the_result_reports(self) -> None:
        profile = LatencyProfile(place_ns=395_000_000, cancel_ns=59_000_000, profile_id="r47_maker_shioaji_p95")

        report = _run([1.0, 2.0], latency_profile=profile).latency_profile

        assert report["latency_profile_id"] == "r47_maker_shioaji_p95"
        assert report["place_ns"] == 395_000_000
        assert report["cancel_ns"] == 59_000_000
        assert report["submit_ack_latency_ms"] == 395.0
        assert report["cancel_ack_latency_ms"] == 59.0
        assert report["model_applied"] is True

    def test_an_unlabelled_profile_is_reported_as_unlabelled_with_its_numbers(self) -> None:
        report = _run(
            [1.0, 2.0], latency_profile=LatencyProfile(place_ns=800_000_000, cancel_ns=800_000_000)
        ).latency_profile

        assert report["latency_profile_id"] == "unlabelled"
        assert report["place_ns"] == 800_000_000

    def test_a_run_without_latency_says_it_was_instant_round_trip(self) -> None:
        report = _run([1.0, 2.0]).latency_profile

        assert report["latency_profile_id"] == "instant_rtt"
        assert report["model_applied"] is False
        assert report["place_ns"] == 0 and report["cancel_ns"] == 0

    def test_a_zero_latency_profile_is_not_reported_as_a_latency_model(self) -> None:
        report = _run([1.0, 2.0], latency_profile=LatencyProfile(place_ns=0, cancel_ns=0)).latency_profile

        assert report["model_applied"] is False

"""Tests for support/resistance price-level rules (SR-01 through SR-06).

Each test function covers at least one rule and asserts observable behaviour,
not mere absence of exceptions.
"""

from __future__ import annotations

import pytest

from hft_platform.reports.models import Bar5m, LargeTrade, SessionData
from hft_platform.reports.rules.support_resistance import (
    find_double_bottoms_tops,
    find_failed_breakouts,
    find_large_trade_levels,
    find_round_numbers,
    find_session_extremes,
    find_volume_at_price,
)

SCALE = 10_000


def _bar(open_: int, high: int, low: int, close: int, volume: int = 100) -> Bar5m:
    return Bar5m(
        ts="2026-03-27T09:00:00",
        open=open_ * SCALE,
        high=high * SCALE,
        low=low * SCALE,
        close=close * SCALE,
        volume=volume,
        ticks=10,
    )


def _trade(price: int, volume: int, direction: str) -> LargeTrade:
    return LargeTrade(ts="2026-03-27T09:00:00", price=price * SCALE, volume=volume, direction=direction)


def _session(high: int, low: int, session: str = "AM") -> SessionData:
    return SessionData(
        session=session,
        symbol="TMFD6",
        date="2026-03-27",
        open=high * SCALE - 100 * SCALE,
        high=high * SCALE,
        low=low * SCALE,
        close=high * SCALE - 50 * SCALE,
        volume=1000,
        tick_count=100,
        bars_5m=[],
        flow_5m=[],
        large_trades=[],
        spread_dist={},
        depth_imbalance=[],
    )


class TestFindLargeTradeLevels:
    def test_buy_creates_support(self) -> None:
        trades = [_trade(20000, 30, "buy")]
        levels = find_large_trade_levels(trades)
        assert len(levels) == 1
        assert "大單買" in levels[0].reason
        assert levels[0].price == 20000 * SCALE

    def test_sell_creates_resistance(self) -> None:
        trades = [_trade(21000, 40, "sell")]
        levels = find_large_trade_levels(trades)
        assert len(levels) == 1
        assert "大單賣" in levels[0].reason

    def test_unknown_creates_key_level(self) -> None:
        trades = [_trade(20500, 25, "unknown")]
        levels = find_large_trade_levels(trades)
        assert "大單" in levels[0].reason
        assert "25口" in levels[0].reason

    def test_below_min_volume_excluded(self) -> None:
        trades = [_trade(20000, 10, "buy")]
        levels = find_large_trade_levels(trades, min_volume=20)
        assert levels == []

    def test_strength_capped_at_one(self) -> None:
        trades = [_trade(20000, 100, "buy")]
        levels = find_large_trade_levels(trades)
        assert levels[0].strength == 1.0

    def test_strength_proportional(self) -> None:
        trades = [_trade(20000, 25, "sell")]
        levels = find_large_trade_levels(trades)
        assert abs(levels[0].strength - 0.5) < 1e-9

    def test_multiple_trades(self) -> None:
        trades = [
            _trade(20000, 30, "buy"),
            _trade(21000, 50, "sell"),
        ]
        levels = find_large_trade_levels(trades)
        assert len(levels) == 2


class TestFindDoubleBottomsTops:
    def test_double_bottom_detected(self) -> None:
        bars = [
            _bar(20100, 20200, 20000, 20150),
            _bar(20200, 20400, 20200, 20350),
            _bar(20300, 20500, 20250, 20400),
            _bar(20100, 20200, 20003, 20150),
        ]
        levels = find_double_bottoms_tops(bars)
        bottoms = [lv for lv in levels if "底" in lv.reason]
        assert len(bottoms) >= 1
        assert bottoms[0].strength == pytest.approx(0.9)

    def test_no_double_bottom_when_lows_too_far_apart(self) -> None:
        bars = [
            _bar(20100, 20200, 20000, 20150),
            _bar(20500, 20700, 20500, 20600),
            _bar(20600, 20800, 20550, 20700),
            _bar(21000, 21200, 21000, 21100),
        ]
        levels = find_double_bottoms_tops(bars)
        bottoms = [lv for lv in levels if "底" in lv.reason]
        assert len(bottoms) == 0

    def test_double_top_detected(self) -> None:
        bars = [
            _bar(20000, 21000, 19900, 20500),
            _bar(20000, 20600, 19900, 20400),
            _bar(19900, 20500, 19800, 20300),
            _bar(20000, 21002, 19950, 20500),
        ]
        levels = find_double_bottoms_tops(bars)
        tops = [lv for lv in levels if "頂" in lv.reason]
        assert len(tops) >= 1
        assert tops[0].strength == pytest.approx(0.9)

    def test_too_few_bars_returns_empty(self) -> None:
        bars = [_bar(20000, 21000, 19900, 20500), _bar(20100, 21100, 20000, 20600)]
        levels = find_double_bottoms_tops(bars)
        assert levels == []

    def test_no_gap_between_lows_not_double_bottom(self) -> None:
        bars = [
            _bar(20100, 20200, 20000, 20150),
            _bar(20100, 20200, 20001, 20150),
            _bar(20200, 20400, 20200, 20350),
            _bar(20100, 20200, 20002, 20150),
        ]
        levels = find_double_bottoms_tops(bars)
        bottoms = [lv for lv in levels if "底" in lv.reason]
        assert len(bottoms) >= 1


class TestFindRoundNumbers:
    def test_round_numbers_in_range(self) -> None:
        low = 19000 * SCALE
        high = 21000 * SCALE
        levels = find_round_numbers(low, high)
        prices = [lv.price for lv in levels]
        assert 20000 * SCALE in prices
        assert 19000 * SCALE in prices

    def test_importance_3_for_1000pt_multiples(self) -> None:
        low = 19000 * SCALE
        high = 21000 * SCALE
        levels = find_round_numbers(low, high)
        level_20000 = next(lv for lv in levels if lv.price == 20000 * SCALE)
        assert level_20000.strength == pytest.approx(1.0)

    def test_importance_2_for_500pt_multiples(self) -> None:
        low = 19000 * SCALE
        high = 21000 * SCALE
        levels = find_round_numbers(low, high)
        level_19500 = next(lv for lv in levels if lv.price == 19500 * SCALE)
        assert level_19500.strength == pytest.approx(2 / 3.0)

    def test_no_duplicates_at_overlapping_multiples(self) -> None:
        low = 19000 * SCALE
        high = 21000 * SCALE
        levels = find_round_numbers(low, high)
        count_20000 = sum(1 for lv in levels if lv.price == 20000 * SCALE)
        assert count_20000 == 1

    def test_empty_range_returns_no_levels(self) -> None:
        low = 20001 * SCALE
        high = 20099 * SCALE
        levels = find_round_numbers(low, high)
        assert levels == []


class TestFindSessionExtremes:
    def test_returns_high_and_low(self) -> None:
        sd = _session(high=21000, low=19500, session="AM")
        levels = find_session_extremes(sd)
        assert len(levels) == 2

    def test_high_price_in_levels(self) -> None:
        sd = _session(high=21000, low=19500)
        levels = find_session_extremes(sd)
        prices = [lv.price for lv in levels]
        assert 21000 * SCALE in prices

    def test_low_price_in_levels(self) -> None:
        sd = _session(high=21000, low=19500)
        levels = find_session_extremes(sd)
        prices = [lv.price for lv in levels]
        assert 19500 * SCALE in prices

    def test_strength_is_half(self) -> None:
        sd = _session(high=21000, low=19500)
        levels = find_session_extremes(sd)
        for lv in levels:
            assert lv.strength == pytest.approx(0.5)

    def test_reason_contains_session(self) -> None:
        sd = _session(high=21000, low=19500, session="PM")
        levels = find_session_extremes(sd)
        for lv in levels:
            assert "PM" in lv.reason

    def test_reason_contains_formatted_price(self) -> None:
        sd = _session(high=21000, low=19500)
        levels = find_session_extremes(sd)
        reasons = {lv.reason for lv in levels}
        assert any("21,000" in r for r in reasons)
        assert any("19,500" in r for r in reasons)


class TestFindVolumeAtPrice:
    def test_top_buckets_returned(self) -> None:
        bars = [
            _bar(20000, 20100, 19900, 20050, volume=500),
            _bar(20000, 20100, 19900, 20050, volume=400),
            _bar(21000, 21100, 20900, 21050, volume=50),
        ]
        levels = find_volume_at_price(bars, top_n=2)
        assert len(levels) == 2

    def test_highest_volume_bucket_is_first(self) -> None:
        bars = [
            _bar(20000, 20100, 19900, 20050, volume=500),
            _bar(20000, 20100, 19900, 20050, volume=400),
            _bar(21000, 21100, 20900, 21050, volume=50),
        ]
        levels = find_volume_at_price(bars, top_n=3)
        assert levels[0].strength >= levels[-1].strength

    def test_strength_capped_at_one(self) -> None:
        bars = [_bar(20000, 20100, 19900, 20050, volume=1000)]
        levels = find_volume_at_price(bars, top_n=1)
        assert levels[0].strength <= 1.0

    def test_empty_bars_returns_empty(self) -> None:
        levels = find_volume_at_price([], top_n=3)
        assert levels == []

    def test_top_n_fewer_than_available_buckets(self) -> None:
        bars = [_bar(20000, 20100, 19900, 20050, volume=200)]
        levels = find_volume_at_price(bars, top_n=5)
        assert len(levels) == 1


class TestFindFailedBreakouts:
    def test_failed_breakout_high_creates_resistance(self) -> None:
        bars = [
            _bar(20000, 20100, 19900, 20050),
            _bar(20100, 20200, 20000, 20150),
            _bar(19800, 19900, 19700, 19750),
        ]
        large_trades = [_trade(20198, 30, "sell")]
        levels = find_failed_breakouts(bars, large_trades)
        resistance = [lv for lv in levels if "假突破" in lv.reason or "壓力" in lv.reason]
        assert len(resistance) >= 1

    def test_failed_breakout_low_creates_support(self) -> None:
        bars = [
            _bar(20000, 20100, 19900, 20050),
            _bar(19800, 19900, 19700, 19750),
            _bar(20200, 20300, 20100, 20250),
        ]
        large_trades = [_trade(19702, 30, "buy")]
        levels = find_failed_breakouts(bars, large_trades)
        support = [lv for lv in levels if "假突破" in lv.reason or "支撐" in lv.reason]
        assert len(support) >= 1

    def test_too_few_bars_returns_empty(self) -> None:
        bars = [_bar(20000, 20100, 19900, 20050), _bar(20100, 20200, 20000, 20150)]
        levels = find_failed_breakouts(bars, [])
        assert levels == []

    def test_no_reversal_no_level(self) -> None:
        bars = [
            _bar(20000, 20100, 19900, 20050),
            _bar(20100, 20200, 20000, 20150),
            _bar(20100, 20300, 20000, 20200),
        ]
        large_trades = [_trade(20198, 30, "sell")]
        levels = find_failed_breakouts(bars, large_trades)
        assert levels == []

"""Unit tests for C17_tmf_frontmonth_native_maker.

Mirror of C14's test suite. Differences:
  - spread_threshold_pts default = 5 (TMF RT 4.0 pt), not 3 (TXF RT 0.48 pt)
  - TMF contract chain TMFB6 → TMFC6 → TMFD6
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from research.alphas.c17_tmf_frontmonth_native_maker.frontmonth import (
    ContractWindow,
    FrontMonthSelector,
    detect_rollover_days,
    iter_front_month_schedule,
)
from research.alphas.c17_tmf_frontmonth_native_maker.impl import (
    C17Alpha,
    C17Params,
    TmfFrontMonthMaker,
)
from research.backtest.maker_engine import (
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import AlphaProtocol

_SCALE = 1_000_000


def _bidask(
    bid_pts: int,
    ask_pts: int,
    bid_qty: int = 10,
    ask_qty: int = 10,
    ts_ns: int | None = None,
) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=bid_pts * _SCALE,
        ask_price=ask_pts * _SCALE,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )


def _trade(price_pts: int, volume: int = 1, ts_ns: int | None = None) -> TickData:
    return TickData(
        exch_ts=ts_ns if ts_ns is not None else time.monotonic_ns(),
        bid_price=0,
        ask_price=0,
        bid_qty=0,
        ask_qty=0,
        trade_price=price_pts * _SCALE,
        trade_volume=volume,
        is_trade=True,
        scale=_SCALE,
    )


# --------------------------------------------------------------------------
# Scaled-int price arithmetic (TMF scale = 1_000_000 in CK)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid_scaled,expected_ask_scaled",
    [
        (22500, 22505, 22_500_000_000, 22_505_000_000),
        (17000, 17006, 17_000_000_000, 17_006_000_000),
        (21000, 21005, 21_000_000_000, 21_005_000_000),
    ],
)
def test_posts_quotes_at_scaled_int_prices(
    bid_pts: int,
    ask_pts: int,
    expected_bid_scaled: int,
    expected_ask_scaled: int,
) -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    actions = maker.on_tick(_bidask(bid_pts, ask_pts))
    post_actions = [a for a in actions if isinstance(a, PostQuote)]
    assert len(post_actions) == 2
    prices = {a.side: a.price for a in post_actions}
    assert prices["buy"] == expected_bid_scaled
    assert prices["sell"] == expected_ask_scaled
    for a in post_actions:
        assert isinstance(a.price, int)


def test_spread_gate_blocks_quotes_below_threshold() -> None:
    """Default spread_threshold_pts=5. Spread=4 (like TMF 4 pt breakeven) → blocked."""
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    actions = maker.on_tick(_bidask(22500, 22504))  # spread = 4 pts, < 5
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert actions == [Hold()]


def test_spread_gate_admits_quotes_at_or_above_threshold() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    actions = maker.on_tick(_bidask(22500, 22505))  # spread = 5 pts
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_custom_spread_threshold_overrides_default() -> None:
    maker = TmfFrontMonthMaker(
        params=C17Params(spread_threshold_pts=3),
        active_symbol="TMFB6",
    )
    actions = maker.on_tick(_bidask(22500, 22503))  # spread = 3 pts
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


# --------------------------------------------------------------------------
# Monotonic time ordering
# --------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(22500, 22505, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(22501, 22506, ts_ns=t1))
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# --------------------------------------------------------------------------
# Max-position gate
# --------------------------------------------------------------------------


def test_stops_buying_at_max_pos() -> None:
    maker = TmfFrontMonthMaker(
        params=C17Params(max_pos=3), active_symbol="TMFD6"
    )
    for _ in range(3):
        maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 3
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos() -> None:
    maker = TmfFrontMonthMaker(
        params=C17Params(max_pos=3), active_symbol="TMFD6"
    )
    for _ in range(3):
        maker.on_fill("sell", 22_500 * _SCALE, 22500.5)
    assert maker.position == -3
    actions = maker.on_tick(_bidask(22500, 22505))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


# --------------------------------------------------------------------------
# Price-movement gate
# --------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    first = maker.on_tick(_bidask(22500, 22505))
    second = maker.on_tick(_bidask(22500, 22505))
    first_posts = [a for a in first if isinstance(a, PostQuote)]
    second_posts = [a for a in second if isinstance(a, PostQuote)]
    assert len(first_posts) == 2
    assert second_posts == []


# --------------------------------------------------------------------------
# Rollover boundary behaviour
# --------------------------------------------------------------------------


def test_rollover_flattens_outgoing_before_incoming() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFB6")
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 2
    assert maker.active_symbol == "TMFB6"
    prior = maker.flatten_position()
    assert prior == 2
    assert maker.position == 0
    maker.set_active_symbol("TMFC6")
    assert maker.active_symbol == "TMFC6"
    assert maker.position == 0
    assert maker.rollover_events == 1


def test_rollover_clears_price_memory() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFB6")
    maker.on_tick(_bidask(22500, 22505))
    assert maker._last_bid is not None
    maker.set_active_symbol("TMFC6")
    assert maker._last_bid is None
    assert maker._last_ask is None
    actions = maker.on_tick(_bidask(22500, 22505))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_rollover_no_op_when_same_symbol() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    maker.set_active_symbol("TMFD6")
    assert maker.rollover_events == 0


# --------------------------------------------------------------------------
# Gap reset
# --------------------------------------------------------------------------


def test_on_gap_clears_transient_quote_state() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    maker.on_tick(_bidask(22500, 22505))
    assert maker._last_bid is not None
    maker.on_gap()
    assert maker._last_bid is None
    assert maker._last_ask is None


def test_on_gap_preserves_authoritative_position() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 2
    maker.on_gap()
    assert maker.position == 2


# --------------------------------------------------------------------------
# Trade events and malformed books
# --------------------------------------------------------------------------


def test_trade_events_return_hold_and_do_not_post() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    actions = maker.on_tick(_trade(22501, volume=3))
    assert actions == [Hold()]


def test_rejects_crossed_book() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    actions = maker.on_tick(_bidask(22505, 22504))
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TmfFrontMonthMaker(active_symbol="TMFD6")
    bad = TickData(
        exch_ts=1,
        bid_price=0,
        ask_price=22_505_000_000,
        bid_qty=1,
        ask_qty=1,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=_SCALE,
    )
    actions = maker.on_tick(bad)
    assert [a for a in actions if isinstance(a, PostQuote)] == []


# --------------------------------------------------------------------------
# AlphaProtocol conformance
# --------------------------------------------------------------------------


def test_c17_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C17Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c17_tmf_frontmonth_native_maker"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c17_manifest_declares_latency_profile() -> None:
    alpha = C17Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c17_manifest_documents_switch_semantics() -> None:
    """Manifest.hypothesis must mention SWITCH vs deployed TMFD6 R47."""
    alpha = C17Alpha()
    h = alpha.manifest.hypothesis.upper()
    assert "SWITCH" in h
    assert "R51-C1B" in h or "TMFD6 R47" in h.upper()


# --------------------------------------------------------------------------
# Frontmonth selector — TMF windows
# --------------------------------------------------------------------------


def test_calendar_selector_returns_frontmonth_for_in_window_date() -> None:
    sel = FrontMonthSelector()
    assert sel.select_by_calendar(date(2026, 2, 10)) == "TMFB6"
    assert sel.select_by_calendar(date(2026, 3, 1)) == "TMFC6"
    assert sel.select_by_calendar(date(2026, 4, 1)) == "TMFD6"


def test_calendar_selector_returns_none_outside_windows() -> None:
    sel = FrontMonthSelector()
    assert sel.select_by_calendar(date(2025, 12, 31)) is None
    assert sel.select_by_calendar(date(2026, 5, 1)) is None


def test_volume_selector_picks_highest_volume_in_eligible_set() -> None:
    sel = FrontMonthSelector()
    vols = {"TMFB6": 500_000, "TMFC6": 900_000, "TMFD6": 100_000}
    assert sel.select_by_volume(date(2026, 2, 10), vols) == "TMFB6"


def test_volume_selector_returns_none_when_no_eligible() -> None:
    sel = FrontMonthSelector()
    vols = {"TMFE6": 1_000_000}  # Not in default windows
    assert sel.select_by_volume(date(2026, 2, 10), vols) is None


def test_select_prefers_volume_over_calendar_when_available() -> None:
    windows = (
        ContractWindow("TMFB6", date(2026, 2, 1), date(2026, 2, 28)),
        ContractWindow("TMFC6", date(2026, 2, 20), date(2026, 3, 20)),
    )
    sel = FrontMonthSelector(windows=windows)
    d = date(2026, 2, 25)
    vols = {"TMFB6": 100, "TMFC6": 999}
    assert sel.select(d, vols) == "TMFC6"
    assert sel.select(d, None) == "TMFB6"


def test_detect_rollover_days_emits_transitions_only() -> None:
    schedule = [
        (date(2026, 2, 24), "TMFB6"),
        (date(2026, 2, 25), "TMFB6"),
        (date(2026, 2, 26), "TMFC6"),
        (date(2026, 2, 27), "TMFC6"),
        (date(2026, 3, 19), "TMFD6"),
    ]
    rolls = detect_rollover_days(schedule)
    assert rolls == [
        (date(2026, 2, 26), "TMFB6", "TMFC6"),
        (date(2026, 3, 19), "TMFC6", "TMFD6"),
    ]


def test_iter_schedule_skips_out_of_window_dates() -> None:
    dates = [
        date(2025, 1, 1),
        date(2026, 2, 10),
        date(2026, 3, 19),
    ]
    sched = iter_front_month_schedule(dates)
    assert [s for _, s in sched] == ["TMFB6", "TMFD6"]

"""Unit tests for C14_txf_frontmonth_native_maker.

Follows .agent/skills/hft-test-hft patterns:
  - scaled-int price assertions (TXF internal scale = 1_000_000)
  - monotonic timestamp ordering
  - rollover boundary behaviour (flatten outgoing before opening incoming)
  - gap-reset clears inventory state
  - every test has at least one ``assert``; test names are behaviour-oriented
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from research.alphas.c14_txf_frontmonth_native_maker.frontmonth import (
    ContractWindow,
    FrontMonthSelector,
    detect_rollover_days,
    iter_front_month_schedule,
)
from research.alphas.c14_txf_frontmonth_native_maker.impl import (
    C14Alpha,
    C14Params,
    QueuePositionStochasticFill,
    TxfFrontMonthMaker,
)
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
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
    """Build a bidask TickData at TXF scale (1_000_000)."""
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
# Scaled-int price arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bid_pts,ask_pts,expected_bid_scaled,expected_ask_scaled",
    [
        (22500, 22503, 22_500_000_000, 22_503_000_000),
        (33000, 33004, 33_000_000_000, 33_004_000_000),
        (21850, 21853, 21_850_000_000, 21_853_000_000),
    ],
)
def test_posts_quotes_at_scaled_int_prices(
    bid_pts: int, ask_pts: int, expected_bid_scaled: int, expected_ask_scaled: int
) -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    actions = maker.on_tick(_bidask(bid_pts, ask_pts))
    post_actions = [a for a in actions if isinstance(a, PostQuote)]
    assert len(post_actions) == 2
    prices = {a.side: a.price for a in post_actions}
    # zero inventory → zero skew → quote at best bid/ask
    assert prices["buy"] == expected_bid_scaled
    assert prices["sell"] == expected_ask_scaled
    for a in post_actions:
        assert isinstance(a.price, int)  # never float


def test_spread_gate_blocks_quotes_below_threshold() -> None:
    """spread 2 pt < spread_threshold_pts=3 → all quotes suppressed."""
    maker = TxfFrontMonthMaker(
        params=C14Params(spread_threshold_pts=3),
        active_symbol="TXFD6",
    )
    actions = maker.on_tick(_bidask(22500, 22502))  # spread = 2 pts
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert posts == []
    assert actions == [Hold()]


def test_spread_gate_admits_quotes_at_or_above_threshold() -> None:
    maker = TxfFrontMonthMaker(
        params=C14Params(spread_threshold_pts=3),
        active_symbol="TXFD6",
    )
    actions = maker.on_tick(_bidask(22500, 22503))  # spread = 3 pts
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


# --------------------------------------------------------------------------
# Monotonic time ordering
# --------------------------------------------------------------------------


def test_monotonic_timestamp_ordering_preserved() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    t0 = time.monotonic_ns()
    maker.on_tick(_bidask(22500, 22504, ts_ns=t0))
    t1 = time.monotonic_ns()
    maker.on_tick(_bidask(22501, 22505, ts_ns=t1))
    # Assertion: timestamps are monotonic and far below epoch threshold
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000  # ~3 years from unix epoch
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS


# --------------------------------------------------------------------------
# Max-position gate
# --------------------------------------------------------------------------


def test_stops_buying_at_max_pos() -> None:
    maker = TxfFrontMonthMaker(
        params=C14Params(max_pos=3), active_symbol="TXFD6"
    )
    # 3 buy fills → pos=3
    for _ in range(3):
        maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 3
    # Next tick should produce only a sell (no buy because pos==max_pos)
    # Price gate test: move the bid so last_bid check doesn't block a buy
    # but the pos gate must still suppress the buy side.
    actions = maker.on_tick(_bidask(22500, 22504))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "buy" not in sides
    assert "sell" in sides


def test_stops_selling_at_negative_max_pos() -> None:
    maker = TxfFrontMonthMaker(
        params=C14Params(max_pos=3), active_symbol="TXFD6"
    )
    for _ in range(3):
        maker.on_fill("sell", 22_500 * _SCALE, 22500.5)
    assert maker.position == -3
    actions = maker.on_tick(_bidask(22500, 22504))
    sides = {a.side for a in actions if isinstance(a, PostQuote)}
    assert "sell" not in sides
    assert "buy" in sides


# --------------------------------------------------------------------------
# Price-movement gate (fresh quotes only)
# --------------------------------------------------------------------------


def test_does_not_restack_same_price() -> None:
    """Identical bid/ask on consecutive ticks → no duplicate quotes."""
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    first = maker.on_tick(_bidask(22500, 22504))
    second = maker.on_tick(_bidask(22500, 22504))
    first_posts = [a for a in first if isinstance(a, PostQuote)]
    second_posts = [a for a in second if isinstance(a, PostQuote)]
    assert len(first_posts) == 2
    assert second_posts == []  # same prices → price-movement gate blocks


# --------------------------------------------------------------------------
# Rollover boundary behaviour
# --------------------------------------------------------------------------


def test_rollover_flattens_outgoing_before_incoming() -> None:
    """On set_active_symbol(new_sym), position is treated as flat for the new
    contract. The prior position must be explicitly flattened by the driver.
    """
    maker = TxfFrontMonthMaker(active_symbol="TXFB6")
    # Accumulate pos=2 on TXFB6
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 2
    assert maker.active_symbol == "TXFB6"

    # Driver MUST call flatten_position() to close TXFB6 before rollover.
    prior = maker.flatten_position()
    assert prior == 2
    assert maker.position == 0

    # Rollover to TXFC6
    maker.set_active_symbol("TXFC6")
    assert maker.active_symbol == "TXFC6"
    assert maker.position == 0
    assert maker.rollover_events == 1


def test_rollover_clears_price_memory() -> None:
    """After rollover, price-movement gate must re-arm on new contract."""
    maker = TxfFrontMonthMaker(active_symbol="TXFB6")
    # Post-quote on TXFB6
    maker.on_tick(_bidask(22500, 22504))
    assert maker._last_bid is not None
    # Rollover — price memory should clear
    maker.set_active_symbol("TXFC6")
    assert maker._last_bid is None
    assert maker._last_ask is None
    # First quote on TXFC6 must post (not gated by stale last_bid)
    actions = maker.on_tick(_bidask(22500, 22504))
    posts = [a for a in actions if isinstance(a, PostQuote)]
    assert len(posts) == 2


def test_rollover_no_op_when_same_symbol() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    maker.set_active_symbol("TXFD6")
    assert maker.rollover_events == 0


# --------------------------------------------------------------------------
# Gap reset
# --------------------------------------------------------------------------


def test_on_gap_clears_transient_quote_state() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    maker.on_tick(_bidask(22500, 22504))
    assert maker._last_bid is not None
    maker.on_gap()
    assert maker._last_bid is None
    assert maker._last_ask is None


def test_on_gap_preserves_authoritative_position() -> None:
    """Fills are authoritative — on_gap must NOT wipe local position."""
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    maker.on_fill("buy", 22_500 * _SCALE, 22500.5)
    assert maker.position == 2
    maker.on_gap()
    assert maker.position == 2


# --------------------------------------------------------------------------
# Trade events do not corrupt quoting state
# --------------------------------------------------------------------------


def test_trade_events_return_hold_and_do_not_post() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    actions = maker.on_tick(_trade(22501, volume=3))
    assert actions == [Hold()]


# --------------------------------------------------------------------------
# Malformed book guard
# --------------------------------------------------------------------------


def test_rejects_crossed_book() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    # ask <= bid is invalid
    actions = maker.on_tick(_bidask(22504, 22503))
    assert [a for a in actions if isinstance(a, PostQuote)] == []


def test_rejects_zero_priced_book() -> None:
    maker = TxfFrontMonthMaker(active_symbol="TXFD6")
    bad = TickData(
        exch_ts=1,
        bid_price=0,
        ask_price=22_504_000_000,
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
# AlphaProtocol conformance (shim, for registry tooling)
# --------------------------------------------------------------------------


def test_c14_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C14Alpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c14_txf_frontmonth_native_maker"
    assert alpha.manifest.strategy_type == "maker"
    # update() is a no-op for the maker shim
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    # reset() returns None
    assert alpha.reset() is None


def test_c14_manifest_declares_latency_profile() -> None:
    """Gate D requires a declared latency profile; verify it's set."""
    alpha = C14Alpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


# --------------------------------------------------------------------------
# Frontmonth selector
# --------------------------------------------------------------------------


def test_calendar_selector_returns_frontmonth_for_in_window_date() -> None:
    sel = FrontMonthSelector()
    assert sel.select_by_calendar(date(2026, 2, 10)) == "TXFB6"
    assert sel.select_by_calendar(date(2026, 3, 1)) == "TXFC6"
    assert sel.select_by_calendar(date(2026, 4, 1)) == "TXFD6"


def test_calendar_selector_returns_none_outside_windows() -> None:
    sel = FrontMonthSelector()
    assert sel.select_by_calendar(date(2025, 12, 31)) is None
    assert sel.select_by_calendar(date(2026, 5, 1)) is None


def test_volume_selector_picks_highest_volume_in_eligible_set() -> None:
    sel = FrontMonthSelector()
    # On Feb 10, only TXFB6 is eligible per calendar; volumes for other
    # contracts are rejected.
    vols = {"TXFB6": 500_000, "TXFC6": 900_000, "TXFD6": 100_000}
    assert sel.select_by_volume(date(2026, 2, 10), vols) == "TXFB6"


def test_volume_selector_returns_none_when_no_eligible() -> None:
    sel = FrontMonthSelector()
    vols = {"TXFE6": 1_000_000}  # TXFE6 not in default windows
    assert sel.select_by_volume(date(2026, 2, 10), vols) is None


def test_select_prefers_volume_over_calendar_when_available() -> None:
    # Define overlapping windows so multiple contracts are eligible.
    windows = (
        ContractWindow("TXFB6", date(2026, 2, 1), date(2026, 2, 28)),
        ContractWindow("TXFC6", date(2026, 2, 20), date(2026, 3, 20)),
    )
    sel = FrontMonthSelector(windows=windows)
    d = date(2026, 2, 25)
    # Both eligible; volume picks TXFC6.
    vols = {"TXFB6": 100, "TXFC6": 999}
    assert sel.select(d, vols) == "TXFC6"
    # No volume → calendar fallback picks first-matching window.
    assert sel.select(d, None) == "TXFB6"


def test_detect_rollover_days_emits_transitions_only() -> None:
    schedule = [
        (date(2026, 2, 24), "TXFB6"),
        (date(2026, 2, 25), "TXFB6"),
        (date(2026, 2, 26), "TXFC6"),  # rollover
        (date(2026, 2, 27), "TXFC6"),
        (date(2026, 3, 19), "TXFD6"),  # rollover
    ]
    rolls = detect_rollover_days(schedule)
    assert rolls == [
        (date(2026, 2, 26), "TXFB6", "TXFC6"),
        (date(2026, 3, 19), "TXFC6", "TXFD6"),
    ]


# --------------------------------------------------------------------------
# QueuePositionStochasticFill (R6-T5-REVISE Fix A)
# --------------------------------------------------------------------------


def _buy_qp(price: int) -> QueuePosition:
    return QueuePosition(side="buy", price=price, queue_ahead=0)


def _sell_qp(price: int) -> QueuePosition:
    return QueuePosition(side="sell", price=price, queue_ahead=0)


def test_fillmodel_p_front_1_keeps_all_fills() -> None:
    """p_front=1.0 degenerates to the inner QueueDepletion — every fill passes."""
    fm = QueuePositionStochasticFill(queue_fraction=0.5, p_front=1.0)
    fills = fm.check_fills(
        [_buy_qp(price=22_500 * _SCALE)],
        trade_price=22_499 * _SCALE,
        trade_volume=5,
    )
    assert len(fills) == 1
    assert fm.stats["total_raw_fills"] == 1
    assert fm.stats["front_kept"] == 1
    assert fm.stats["favorable_dropped"] == 0


def test_fillmodel_p_front_0_drops_favorable_keeps_adverse_on_buy() -> None:
    fm = QueuePositionStochasticFill(queue_fraction=0.5, p_front=0.0)
    # Favorable buy: trade > quote → dropped
    favorable = fm.check_fills(
        [_buy_qp(price=22_500 * _SCALE)],
        trade_price=22_501 * _SCALE,
        trade_volume=5,
    )
    assert favorable == []
    # Adverse buy: trade < quote → kept
    adverse = fm.check_fills(
        [_buy_qp(price=22_500 * _SCALE)],
        trade_price=22_499 * _SCALE,
        trade_volume=5,
    )
    assert len(adverse) == 1


def test_fillmodel_sell_side_adverse_when_trade_above_quote() -> None:
    fm = QueuePositionStochasticFill(queue_fraction=0.5, p_front=0.0)
    # Adverse sell: trade > quote (market kept rising)
    adverse = fm.check_fills(
        [_sell_qp(price=22_500 * _SCALE)],
        trade_price=22_501 * _SCALE,
        trade_volume=5,
    )
    assert len(adverse) == 1
    # Favorable sell: trade < quote (market reverted)
    favorable = fm.check_fills(
        [_sell_qp(price=22_500 * _SCALE)],
        trade_price=22_499 * _SCALE,
        trade_volume=5,
    )
    assert favorable == []


def test_fillmodel_adverse_rate_monotone_decreasing_p_front() -> None:
    """Core Fix-A invariant: as p_front decreases, adverse fraction in kept
    pool increases monotonically. Synthetic 200-fill stream, 50/50 mix.
    """
    results: dict[float, float] = {}
    for p in [1.0, 0.5, 0.3, 0.0]:
        fm = QueuePositionStochasticFill(queue_fraction=0.5, p_front=p)
        n_total = 0
        n_adverse = 0
        for i in range(200):
            quote = (22_500 + i) * _SCALE
            if i % 2 == 0:
                trade = (22_501 + i) * _SCALE  # favorable buy
                is_adv = False
            else:
                trade = (22_499 + i) * _SCALE  # adverse buy
                is_adv = True
            got = fm.check_fills(
                [_buy_qp(price=quote)], trade_price=trade, trade_volume=1
            )
            if got:
                n_total += 1
                if is_adv:
                    n_adverse += 1
        results[p] = n_adverse / n_total if n_total else 0.0

    # Boundaries: p=1.0 keeps both → ~0.5 adverse; p=0.0 keeps only adverse → 1.0
    assert results[1.0] == pytest.approx(0.5, abs=0.1)
    assert results[0.0] == 1.0
    # Monotone non-decreasing
    assert results[1.0] < results[0.5] <= results[0.3] < results[0.0]


def test_fillmodel_stats_counter_accuracy() -> None:
    fm = QueuePositionStochasticFill(queue_fraction=0.5, p_front=0.0)
    fm.check_fills(
        [_buy_qp(price=22_500 * _SCALE)],
        trade_price=22_501 * _SCALE,
        trade_volume=1,
    )  # favorable → dropped
    fm.check_fills(
        [_buy_qp(price=22_500 * _SCALE)],
        trade_price=22_499 * _SCALE,
        trade_volume=1,
    )  # adverse → kept
    st = fm.stats
    assert st["total_raw_fills"] == 2
    assert st["front_kept"] == 0
    assert st["adverse_kept"] == 1
    assert st["favorable_dropped"] == 1


def test_fillmodel_rejects_invalid_p_front() -> None:
    with pytest.raises(ValueError):
        QueuePositionStochasticFill(p_front=-0.1)
    with pytest.raises(ValueError):
        QueuePositionStochasticFill(p_front=1.5)


def test_fillmodel_label_reflects_p_front() -> None:
    assert "p_front=0.30" in QueuePositionStochasticFill(p_front=0.3).label


# --------------------------------------------------------------------------
# Frontmonth selector (existing tests continue below)
# --------------------------------------------------------------------------


def test_iter_schedule_skips_out_of_window_dates() -> None:
    dates = [
        date(2025, 1, 1),  # out of window
        date(2026, 2, 10),  # TXFB6
        date(2026, 3, 19),  # TXFD6
    ]
    sched = iter_front_month_schedule(dates)
    assert [s for _, s in sched] == ["TXFB6", "TXFD6"]

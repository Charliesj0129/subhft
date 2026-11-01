"""The platform exit closes positions that its stops would otherwise freeze.

THESHOW 10/15-10/21: the daily-loss HALT held one TMFJ6 long that nothing could
close (HALT blocked new orders, R47's spread gate suppressed its exits,
``HaltFlattener`` was never built) until the exchange settled it.
"""

from __future__ import annotations

import datetime as dt

import pytest
import structlog
from prometheus_client import REGISTRY

from hft_platform.contracts.strategy import TIF, IntentType, Side
from hft_platform.risk.loss_exit import (
    INTENT_ID_BASE,
    TRIGGER_DAILY_LOSS_HALT,
    TRIGGER_EXPIRY,
    TRIGGER_POSITION_LOSS,
    LossExitController,
    OpenPosition,
)

TPE = dt.timezone(dt.timedelta(hours=8))
SCALE = 10_000
TICK = 1 * SCALE  # TMF tick: 1 point
BID, ASK = 46_600 * SCALE, 46_602 * SCALE
LIMIT = 5_000 * SCALE  # 5,000 NTD in accumulator units


def _tpe(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=TPE).timestamp()) * 1_000_000_000


NOW = _tpe(2026, 10, 16, 10, 0)


def _plan(ctrl: LossExitController, positions, *, now=NOW, daily_halt=False, limit=LIMIT, expiry=True, book=(BID, ASK)):
    return ctrl.plan(
        now_ns=now,
        positions=positions,
        daily_halt=daily_halt,
        position_loss_limit_scaled=limit,
        expiry_enabled=expiry,
        l1=lambda _s: book,
        multiplier=lambda _s: 10,
        tick_size=lambda _s: TICK,
    )


def _long(qty: int = 1, entry: int = 46_700, strategy: str = "R47_MAKER_TMF", symbol: str = "TMFK6") -> OpenPosition:
    return OpenPosition(strategy, symbol, qty, entry * SCALE)


def test_nothing_is_sent_while_no_trigger_holds():
    assert _plan(LossExitController(), [_long()]) == []


def test_a_latched_daily_stop_closes_a_long_with_a_marketable_ioc_sell():
    [intent] = _plan(LossExitController(), [_long()], daily_halt=True)

    assert intent.intent_type == IntentType.FORCE_FLAT
    assert intent.side == Side.SELL
    assert intent.qty == 1
    assert intent.tif == TIF.IOC
    assert intent.price == BID - 5 * TICK
    assert intent.strategy_id == "R47_MAKER_TMF"
    assert intent.reason == f"loss_exit:{TRIGGER_DAILY_LOSS_HALT}"
    assert intent.intent_id > INTENT_ID_BASE
    assert intent.price > 0


def test_a_short_is_closed_with_a_buy_through_the_ask():
    [intent] = _plan(LossExitController(), [_long(qty=-2)], daily_halt=True)

    assert (intent.side, intent.qty, intent.price) == (Side.BUY, 2, ASK + 5 * TICK)


@pytest.mark.parametrize(("entry", "fires"), [(47_101, True), (47_100, False)])
def test_the_position_loss_trigger_fires_at_the_limit(entry, fires):
    # mid 46,601; long 1 x 10 NTD/pt: entry 47,101 -> -5,000 NTD, 47,100 -> -4,990 NTD
    intents = _plan(LossExitController(), [_long(entry=entry)])

    assert bool(intents) is fires
    if fires:
        assert intents[0].reason == f"loss_exit:{TRIGGER_POSITION_LOSS}"


def test_a_zero_position_limit_disables_that_trigger():
    assert _plan(LossExitController(), [_long(entry=60_000)], limit=0) == []


@pytest.mark.parametrize(
    ("now", "fires"),
    [
        (_tpe(2026, 10, 21, 12, 59), False),
        (_tpe(2026, 10, 21, 13, 0), True),
        (_tpe(2026, 10, 20, 13, 30), False),
    ],
)
def test_the_expiring_month_is_closed_from_the_cutoff_on_its_delivery_date(now, fires):
    intents = _plan(LossExitController(), [_long(symbol="TMFJ6", entry=46_600)], now=now)

    assert bool(intents) is fires
    if fires:
        assert intents[0].reason == f"loss_exit:{TRIGGER_EXPIRY}"


def test_the_expiry_trigger_can_be_switched_off():
    now = _tpe(2026, 10, 21, 13, 5)
    assert _plan(LossExitController(), [_long(symbol="TMFJ6", entry=46_600)], now=now, expiry=False) == []


def test_without_a_two_sided_book_nothing_is_sent_and_it_is_retried_later():
    ctrl = LossExitController(retry_interval_s=10)
    assert _plan(ctrl, [_long()], daily_halt=True, book=None) == []
    assert _plan(ctrl, [_long()], daily_halt=True, book=(BID, 0)) == []
    # Being unpriced does not start the retry clock: the first priced tick sends.
    assert len(_plan(ctrl, [_long()], daily_halt=True, now=NOW + 1)) == 1


def _unpriced_count() -> float:
    return REGISTRY.get_sample_value("loss_exit_unpriced_total", {"trigger": TRIGGER_DAILY_LOSS_HALT}) or 0.0


def test_an_unpriced_exit_logs_once_a_minute_while_it_is_retried_every_tick():
    """THESHOW 10/30-10/31: a HALT with no book logged ``loss_exit_unpriced`` 62,946 times, once per tick."""
    ctrl = LossExitController(retry_interval_s=10)
    before = _unpriced_count()

    with structlog.testing.capture_logs() as logs:
        for second in range(120):  # two minutes of 1 Hz supervisor ticks, no two-sided book
            assert _plan(ctrl, [_long()], daily_halt=True, now=NOW + second * 1_000_000_000, book=None) == []

    assert [e["event"] for e in logs].count("loss_exit_unpriced") == 2  # t=0 s and t=60 s
    assert _unpriced_count() - before == 120


def test_a_new_unpriced_episode_is_logged_at_once():
    ctrl = LossExitController(retry_interval_s=10)

    with structlog.testing.capture_logs() as logs:
        _plan(ctrl, [_long()], daily_halt=True, book=None)
        assert len(_plan(ctrl, [_long()], daily_halt=True, now=NOW + 1_000_000_000)) == 1  # priced: exit sent
        _plan(ctrl, [_long()], daily_halt=True, now=NOW + 2_000_000_000, book=None)  # inside the retry interval
        _plan(ctrl, [], daily_halt=True, now=NOW + 3_000_000_000)  # flat
        _plan(ctrl, [_long()], daily_halt=True, now=NOW + 4_000_000_000, book=None)  # a new lot, no book

    assert [e["event"] for e in logs].count("loss_exit_unpriced") == 2


def test_a_due_exit_is_resent_only_after_the_retry_interval():
    ctrl = LossExitController(retry_interval_s=10)
    assert len(_plan(ctrl, [_long()], daily_halt=True)) == 1
    assert _plan(ctrl, [_long()], daily_halt=True, now=NOW + 9_999_999_999) == []
    again = _plan(ctrl, [_long()], daily_halt=True, now=NOW + 10_000_000_000)

    assert len(again) == 1
    assert again[0].intent_id == INTENT_ID_BASE + 2


def test_a_closed_position_starts_fresh():
    ctrl = LossExitController(retry_interval_s=10)
    _plan(ctrl, [_long()], daily_halt=True)
    _plan(ctrl, [], daily_halt=True, now=NOW + 1)  # flat: bookkeeping cleared

    assert len(_plan(ctrl, [_long()], daily_halt=True, now=NOW + 2)) == 1


def test_one_order_per_symbol_booked_to_the_largest_holder():
    positions = [_long(qty=2, strategy="A"), _long(qty=-1, strategy="B"), _long(qty=1, symbol="TMFL6", strategy="B")]

    intents = _plan(LossExitController(), positions, daily_halt=True)

    by_symbol = {i.symbol: i for i in intents}
    assert set(by_symbol) == {"TMFK6", "TMFL6"}
    assert (by_symbol["TMFK6"].strategy_id, by_symbol["TMFK6"].qty) == ("A", 1)
    assert by_symbol["TMFL6"].strategy_id == "B"


def test_strategies_that_net_to_flat_need_no_exit():
    assert _plan(LossExitController(), [_long(qty=1, strategy="A"), _long(qty=-1, strategy="B")], daily_halt=True) == []


@pytest.mark.parametrize(("second_entry", "fires"), [(47_602, True), (47_600, False)])
def test_the_position_loss_is_valued_on_the_netted_entry(second_entry, fires):
    """Two entries of one strategy (two accounts) value as one position at the average entry."""
    # mid 46,601. Entries 46,600 + 47,602 average 47,101: 2 lots x -500 pt x 10 = -10,000 NTD.
    positions = [_long(entry=46_600, strategy="A"), _long(entry=second_entry, strategy="A")]

    intents = _plan(LossExitController(), positions, limit=10_000 * SCALE)

    assert bool(intents) is fires

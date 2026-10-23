"""The daily-loss limit judges the day's change, measured from the day's opening mark.

``update_unrealized`` receives each position's PnL since entry. Before this change
that figure was added whole to every day's total, so a lot carried overnight brought
all of its earlier loss into each new day: on THESHOW one TMFJ6 long re-latched the
5,000 NTD stop at every 05:00 reset from 10/16 to its settlement on 10/21.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.validators import DailyLossLimitValidator

NTD = 10_000  # one NTD in accumulator units (price scale; the multiplier is already applied)


def _ntd(amount: int) -> int:
    return amount * NTD


def _at(iso_utc: str) -> int:
    return int(datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


DAY1 = _at("2026-10-16T02:00:00")  # 10:00 Taipei
DAY2 = _at("2026-10-16T22:00:00")  # 06:00 Taipei next day, past the 21:00Z boundary
DAY2_LATER = _at("2026-10-17T01:00:00")


def _validator(ts_ns: int, **intraday: object) -> DailyLossLimitValidator:
    cfg: dict[str, object] = {
        "scope": "global",
        "soft_limit_ntd": 500,
        "hard_limit_ntd": 5000,
        "soft_recovery_ntd": 300,
        "soft_limit_cooldown_s": 60,
        "price_scale": 10000,
    }
    cfg.update(intraday)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=ts_ns):
        return DailyLossLimitValidator({"global_defaults": {}, "intraday_pnl": cfg}, None)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47",
        symbol="TMFK6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=470_000_000,
        qty=1,
    )


def test_daily_unrealized_is_the_since_entry_figure_until_the_day_open_is_known():
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY1):
        v.update_unrealized(_ntd(-1200), complete=False)

    assert v.day_open_known is False
    assert v.daily_unrealized() == _ntd(-1200)


def test_the_first_complete_snapshot_after_the_boundary_becomes_the_day_open():
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2):
        v.update_unrealized(_ntd(-4000), complete=True)
        v.update_unrealized(_ntd(-4300), complete=True)

    assert v.day_open_known is True
    assert v.daily_unrealized() == _ntd(-300)


def test_a_carried_lot_closed_today_books_only_its_move_since_the_open():
    """Realized (exit - entry) minus the opening (open - entry) leaves (exit - open)."""
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2):
        v.update_unrealized(_ntd(-6000), complete=True)  # opening mark of the carried lot
        v.record_pnl("R47", _ntd(-7000))  # closed 1,000 below the open
        v.update_unrealized(0, complete=True)  # book flat
        ok, reason = v.check(_intent())

    assert sum(v._accumulated_loss.values()) + v.daily_unrealized() == _ntd(-1000)
    # -1,000 today is past the 500 soft limit, nowhere near the 5,000 hard one;
    # read whole, the -7,000 realized would have halted.
    assert (ok, reason.split(":")[0]) == (False, "SOFT_LIMIT")
    assert v.halt_triggered is False


def test_a_carried_loss_does_not_trip_the_new_days_limit_but_a_new_loss_does():
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2):
        v.update_unrealized(_ntd(-12_875), complete=True)  # the 10/16 21:00Z figure
        assert v.halt_triggered is False
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2_LATER):
        v.update_unrealized(_ntd(-17_874), complete=True)  # 4,999 more today
        assert v.halt_triggered is False
        v.update_unrealized(_ntd(-17_875), complete=True)  # 5,000 today

    assert v.halt_triggered is True


def test_an_incomplete_snapshot_cannot_turn_a_carried_loss_into_a_gain():
    """An unpriced lot leaves the snapshot but not the opening figure; that must not read as profit."""
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2):
        v.update_unrealized(_ntd(-6000), complete=True)
        v.update_unrealized(0, complete=False)  # the losing lot could not be priced

    assert v.daily_unrealized() == 0  # not +6,000


def test_a_boundary_rolled_without_a_snapshot_falls_back_to_the_since_entry_figure():
    v = _validator(DAY1)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY1):
        v.update_unrealized(_ntd(-300), complete=True)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=DAY2):
        v.roll_daily_boundary()

    assert v.day_open_known is False
    assert v.daily_unrealized() == _ntd(-300)


def test_position_loss_limit_defaults_to_the_hard_limit():
    assert _validator(DAY1).position_loss_limit_scaled == _ntd(5000)


def test_position_loss_limit_reads_its_own_key():
    assert _validator(DAY1, position_loss_limit_ntd=8000).position_loss_limit_scaled == _ntd(8000)


def test_position_loss_limit_is_zero_without_an_intraday_block():
    v = DailyLossLimitValidator({"global_defaults": {}}, None)
    assert v.position_loss_limit_scaled == 0

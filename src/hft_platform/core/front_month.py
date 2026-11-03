"""One definition of "the front month" for TAIFEX monthly futures.

Until 2026-11 each consumer answered it on its own: the symbol builder by the
exchange *date*, the universe validator and the strategy family clock by the
13:30 delivery cutoff, ``startup_recon`` and ``risk/expiry`` by the third
Wednesday from the code. They disagreed for ten and a half hours every month
(13:30 to midnight on delivery day, when the builder still chose the settling
contract and the validator refused it), and on holiday months the code-derived
date was a business day early.

This module is that definition, as pure functions of ``now_ns`` and a list of
``(code, delivery_date)`` pairs:

- :func:`front_month` picks the broker's front contract: the earliest delivery
  date that is not before :func:`~hft_platform.core.market_calendar.taifex_delivery_cutoff`.
- :func:`expected_front_delivery` derives the same date from the exchange rule
  alone (third Wednesday, holiday-adjusted), with no broker data.
- :func:`front_month_disagreement` compares the two. A broker list that has
  lost its front month, or a rule that a TAIFEX schedule change has
  invalidated, shows up here instead of as a silent wrong subscription.

Nothing here touches the broker, the SDK, or a clock of its own.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from hft_platform.core.market_calendar import (
    MarketCalendar,
    taifex_delivery_cutoff,
    taifex_monthly_delivery_date,
)

__all__ = [
    "FrontMonth",
    "expected_front_delivery",
    "front_month",
    "front_month_disagreement",
]


@dataclass(frozen=True, slots=True)
class FrontMonth:
    """The front contract of one root at one instant."""

    code: str
    delivery: dt.date


def front_month(contracts: Iterable[tuple[str, dt.date | None]], *, now_ns: int) -> FrontMonth | None:
    """The contract with the earliest delivery date still trading at ``now_ns``.

    ``contracts`` are ``(code, delivery_date)`` pairs for one root. A pair
    without a date is skipped (it cannot be ordered). Ties on the date (the
    month code and its ``R1`` continuous alias share one) go to the code that
    sorts first, so the answer is stable. None when nothing is still trading.
    """
    cutoff = taifex_delivery_cutoff(now_ns)
    best: FrontMonth | None = None
    for code, delivery in contracts:
        if delivery is None or delivery < cutoff:
            continue
        candidate = FrontMonth(code=str(code), delivery=delivery)
        if best is None or (candidate.delivery, candidate.code) < (best.delivery, best.code):
            best = candidate
    return best


def expected_front_delivery(*, now_ns: int, calendar: MarketCalendar | None = None) -> dt.date:
    """The front month's delivery date from the exchange rule, with no broker data.

    This month's delivery date if it is not before the cutoff, otherwise next
    month's. Monthly contracts only: every TAIFEX index future the platform
    trades (TXF, MXF, TMF) lists one contract per calendar month.
    """
    cutoff = taifex_delivery_cutoff(now_ns)
    year, month = cutoff.year, cutoff.month
    delivery = taifex_monthly_delivery_date(year, month, calendar=calendar)
    if delivery >= cutoff:
        return delivery
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return taifex_monthly_delivery_date(year, month, calendar=calendar)


def front_month_disagreement(
    root: str,
    contracts: Iterable[tuple[str, dt.date | None]],
    *,
    now_ns: int,
    calendar: MarketCalendar | None = None,
) -> str | None:
    """Why the broker's front month for ``root`` differs from the rule; None when they agree.

    Disagreement is reported, never resolved: the broker list may be partial
    (2026-11-02 lost every TXF row) or the exchange may have moved a delivery
    date the rule does not know. Either way the caller must not guess.
    """
    expected = expected_front_delivery(now_ns=now_ns, calendar=calendar)
    front = front_month(contracts, now_ns=now_ns)
    if front is None:
        return f"{root}: no contract still trading (rule expects delivery {expected.isoformat()})"
    if front.delivery != expected:
        return (
            f"{root}: broker front {front.code} delivers {front.delivery.isoformat()}, "
            f"rule expects {expected.isoformat()}"
        )
    return None

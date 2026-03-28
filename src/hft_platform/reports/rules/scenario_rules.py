"""Scenario generation rules SC-01 through SC-03.

Each function is pure: given a SignalReport, return a Scenario or None.
All price fields use ScaledPrice (int x10000) per the Precision Law.
"""

from __future__ import annotations

from hft_platform.contracts.types import ScaledPrice
from hft_platform.reports.models import Scenario, SignalReport

__all__ = [
    "scenario_break_below_support",
    "scenario_hold_and_bounce",
    "scenario_range_bound",
]

PLATFORM_SCALE: int = 10_000


def _fmt(price: int) -> str:
    """Format a ScaledPrice as a human-readable integer point value with commas."""
    return f"{price // PLATFORM_SCALE:,}"


def scenario_break_below_support(signal: SignalReport) -> Scenario | None:
    """SC-01: Break below S1 and target S2.

    Requires at least 2 supports. probability is '較高' for bearish bias,
    '較低' otherwise.
    """
    if len(signal.supports) < 2:
        return None

    s1 = signal.supports[0]
    s2 = signal.supports[1]

    probability = "較高" if signal.bias == "bearish" else "較低"
    condition = f"若破 {_fmt(s1.price)}"

    return Scenario(
        id="SC-01",
        label="向下破支撐",
        probability=probability,
        condition=condition,
        target=s2.price,
        description=f"跌破 {_fmt(s1.price)} 支撐後，下一目標 {_fmt(s2.price)}",
    )


def scenario_hold_and_bounce(signal: SignalReport) -> Scenario | None:
    """SC-02: Hold S1 and bounce to R1.

    Requires at least 1 support and 1 resistance. probability is '較低' for
    bearish bias, '較高' otherwise.
    """
    if not signal.supports or not signal.resistances:
        return None

    s1 = signal.supports[0]
    r1 = signal.resistances[0]

    probability = "較低" if signal.bias == "bearish" else "較高"
    condition = f"若守住 {_fmt(s1.price)} 且站回 {_fmt(r1.price)}"

    return Scenario(
        id="SC-02",
        label="守支撐反彈",
        probability=probability,
        condition=condition,
        target=r1.price,
        description=f"守住 {_fmt(s1.price)} 支撐後反彈至 {_fmt(r1.price)}",
    )


def scenario_range_bound(signal: SignalReport) -> Scenario | None:
    """SC-03: Range-bound oscillation between S1 and R1.

    Requires at least 1 support and 1 resistance. probability is always '較低'.
    target is 0 (no directional target in range-bound scenario).
    """
    if not signal.supports or not signal.resistances:
        return None

    s1 = signal.supports[0]
    r1 = signal.resistances[0]

    condition = f"若在 {_fmt(s1.price)}-{_fmt(r1.price)} 之間反覆"

    return Scenario(
        id="SC-03",
        label="區間震盪",
        probability="較低",
        condition=condition,
        target=ScaledPrice(0),
        description=f"在 {_fmt(s1.price)} 至 {_fmt(r1.price)} 區間來回震盪",
    )

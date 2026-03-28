"""ScenarioBuilder: derive a ScenarioReport from a SignalReport.

Orchestrates scenario rules SC-01 through SC-03 and computes trade levels
(entry zone, target, stop loss) from session data.

All price fields use ScaledPrice (int x10000) per the Precision Law.
"""
from __future__ import annotations

from hft_platform.reports.models import KeyLevel, ScenarioReport, SignalReport
from hft_platform.reports.rules.scenario_rules import (
    scenario_break_below_support,
    scenario_hold_and_bounce,
    scenario_range_bound,
)

__all__ = ["ScenarioBuilder"]

_DIRECTION_MAP: dict[str, str] = {
    "bearish": "偏空",
    "bullish": "偏多",
    "neutral": "中性",
}

_SCENARIO_GENERATORS = [
    scenario_break_below_support,
    scenario_hold_and_bounce,
    scenario_range_bound,
]


def _clamp(value: int, lo: int, hi: int) -> int:
    """Clamp value to [lo, hi] inclusive."""
    return max(lo, min(hi, value))


def _importance_from_strength(strength: float) -> int:
    """Convert a strength float [0,1] to importance int [1,3]."""
    return _clamp(int(strength * 3) + 1, 1, 3)


def _compute_trade_levels(
    signal: SignalReport,
) -> tuple[tuple[int, int], int, int]:
    """Return (entry_zone, target, stop_loss) for the primary trade direction.

    ATR proxy = session high - low (ScaledPrice integers).

    Bearish + has resistance:
        entry near R1 (just below), target = S1, stop = R1 + ATR/5

    Bullish + has support:
        entry near S1 (just above), target = R1, stop = S1 - ATR/5

    Else (neutral or missing levels):
        midpoint ± ATR/10
    """
    sd = signal.session_data
    atr = sd.high - sd.low  # ScaledPrice units; may be 0 on flat sessions

    if signal.bias == "bearish" and signal.resistances:
        r1_price = signal.resistances[0].price
        s1_price = signal.supports[0].price if signal.supports else r1_price - atr // 5
        atr_step = atr // 5 if atr else r1_price // 100
        entry_high = r1_price
        entry_low = r1_price - atr_step
        target = s1_price
        stop_loss = r1_price + atr_step
        return (entry_low, entry_high), target, stop_loss

    if signal.bias == "bullish" and signal.supports:
        s1_price = signal.supports[0].price
        r1_price = signal.resistances[0].price if signal.resistances else s1_price + atr // 5
        atr_step = atr // 5 if atr else s1_price // 100
        entry_low = s1_price
        entry_high = s1_price + atr_step
        target = r1_price
        stop_loss = s1_price - atr_step
        return (entry_low, entry_high), target, stop_loss

    # Neutral or no levels available
    midpoint = (sd.high + sd.low) // 2
    atr_step = atr // 10 if atr else midpoint // 200
    entry_low = midpoint - atr_step
    entry_high = midpoint + atr_step
    # Neutral: no strong directional bias; stop = entry_low - step, target = entry_high + step
    target = entry_high + atr_step
    stop_loss = entry_low - atr_step
    return (entry_low, entry_high), target, stop_loss


class ScenarioBuilder:
    """Build a ScenarioReport from a SignalReport.

    Usage::

        report = ScenarioBuilder().build(signal)
    """

    def build(self, signal: SignalReport) -> ScenarioReport:
        """Derive a full ScenarioReport from the given SignalReport."""
        direction = _DIRECTION_MAP.get(signal.bias, "中性")
        confidence_pct = int(50 + signal.bias_confidence * 30)

        # ---------------------------------------------------------------
        # Key levels: top 3 supports → S1/S2/S3, top 3 resistances → R1/R2/R3
        # ---------------------------------------------------------------
        key_levels: list[KeyLevel] = []

        for idx, level in enumerate(signal.supports[:3]):
            label = f"S{idx + 1}"
            key_levels.append(
                KeyLevel(
                    price=level.price,
                    label=label,
                    importance=_importance_from_strength(level.strength),
                    reason=level.reason,
                )
            )

        for idx, level in enumerate(signal.resistances[:3]):
            label = f"R{idx + 1}"
            key_levels.append(
                KeyLevel(
                    price=level.price,
                    label=label,
                    importance=_importance_from_strength(level.strength),
                    reason=level.reason,
                )
            )

        # ---------------------------------------------------------------
        # Scenarios: run all generators, collect non-None results
        # ---------------------------------------------------------------
        scenarios = [
            sc
            for gen in _SCENARIO_GENERATORS
            if (sc := gen(signal)) is not None
        ]

        # ---------------------------------------------------------------
        # Trade levels
        # ---------------------------------------------------------------
        entry_zone, target, stop_loss = _compute_trade_levels(signal)

        return ScenarioReport(
            signal=signal,
            direction=direction,
            confidence_pct=confidence_pct,
            entry_zone=entry_zone,
            target=target,
            stop_loss=stop_loss,
            scenarios=scenarios,
            key_levels=key_levels,
        )

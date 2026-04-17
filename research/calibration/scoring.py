"""CalibrationScore: multi-dimensional fit scoring for exponent calibration.

Scores each dimension 0-1, then combines via weighted composite.
Default weights: fill_rate=0.35, adverse_fill=0.25, pnl_direction=0.25, pnl_magnitude=0.15
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean


@dataclass(frozen=True)
class DailyFillSummary:
    """Per-day aggregate fill metrics for one source (sim or live)."""

    date: str
    n_fills: int
    adverse_pct: float
    pnl: float


@dataclass(frozen=True)
class CalibrationScore:
    """Multi-dimensional calibration fit score."""

    fill_rate_score: float
    adverse_fill_score: float
    pnl_direction_score: float
    pnl_magnitude_score: float

    def composite(self, weights: tuple[float, float, float, float] = (0.35, 0.25, 0.25, 0.15)) -> float:
        """Weighted composite score.

        Default weights: fill_rate (0.35) most important, pnl_magnitude (0.15) least.
        """
        components = (
            self.fill_rate_score,
            self.adverse_fill_score,
            self.pnl_direction_score,
            self.pnl_magnitude_score,
        )
        return sum(s * w for s, w in zip(components, weights, strict=True))

    def to_dict(self) -> dict:
        return asdict(self)


def compute_fill_rate_score(sim: float, live: float) -> float:
    """1 - |sim - live| / live, clipped to [0, 1]."""
    if live <= 0:
        return 0.0
    err = abs(sim - live) / live
    return max(0.0, 1.0 - err)


def compute_adverse_fill_score(sim_pct: float, live_pct: float) -> float:
    """1 - |sim - live| / max(live, 1), clipped to [0, 1]."""
    denom = max(live_pct, 1.0)
    err = abs(sim_pct - live_pct) / denom
    return max(0.0, 1.0 - err)


def compute_pnl_direction_score(sim_pnl: list[float], live_pnl: list[float]) -> float:
    """Fraction of days where sim PnL sign matches live PnL sign."""
    if not sim_pnl or not live_pnl or len(sim_pnl) != len(live_pnl):
        return 0.0
    matches = sum(1 for s, lv in zip(sim_pnl, live_pnl, strict=True)
                   if (s >= 0 and lv >= 0) or (s < 0 and lv < 0))
    return matches / len(sim_pnl)


def compute_pnl_magnitude_score(sim: float, live: float) -> float:
    """1 - |sim - live| / |live|, clipped to [0, 1]."""
    if live == 0:
        return 0.0
    err = abs(sim - live) / abs(live)
    return max(0.0, 1.0 - err)


def compute_score(
    sim_days: list[DailyFillSummary],
    live_days: list[DailyFillSummary],
) -> CalibrationScore:
    """Compute multi-dimensional score from aligned sim/live daily summaries.

    Days must be aligned by date. Missing dates are excluded.
    """
    sim_by_date = {d.date: d for d in sim_days}
    live_by_date = {d.date: d for d in live_days}
    common_dates = sorted(sim_by_date.keys() & live_by_date.keys())

    if not common_dates:
        return CalibrationScore(0.0, 0.0, 0.0, 0.0)

    sim_aligned = [sim_by_date[d] for d in common_dates]
    live_aligned = [live_by_date[d] for d in common_dates]

    fill_rate = compute_fill_rate_score(
        sim=mean(d.n_fills for d in sim_aligned),
        live=mean(d.n_fills for d in live_aligned),
    )
    adverse = compute_adverse_fill_score(
        sim_pct=mean(d.adverse_pct for d in sim_aligned),
        live_pct=mean(d.adverse_pct for d in live_aligned),
    )
    pnl_dir = compute_pnl_direction_score(
        sim_pnl=[d.pnl for d in sim_aligned],
        live_pnl=[d.pnl for d in live_aligned],
    )
    pnl_mag = compute_pnl_magnitude_score(
        sim=sum(d.pnl for d in sim_aligned),
        live=sum(d.pnl for d in live_aligned),
    )

    return CalibrationScore(
        fill_rate_score=fill_rate,
        adverse_fill_score=adverse,
        pnl_direction_score=pnl_dir,
        pnl_magnitude_score=pnl_mag,
    )

"""Held-out validation for exponent calibration.

Given a best candidate from sweep, re-runs it on held-out days and
reports the composite score. Also determines calibration confidence
based on data quantity + score.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from research.calibration.scoring import (
    CalibrationScore,
    DailyFillSummary,
    compute_score,
)
from research.calibration.sweep import QueueModelCandidate, SweepResult


def split_days(
    days: list[str], ratio: float = 0.7,
) -> tuple[list[str], list[str]]:
    """Split days into train/test.

    If >= 10 days: 70/30 split. Otherwise leave-one-out (1 test day).
    """
    if len(days) >= 10:
        n_train = int(len(days) * ratio)
        return days[:n_train], days[n_train:]
    else:
        # LOO: last day is test
        return days[:-1], days[-1:]


def determine_confidence(days: int, score: float) -> Literal["low", "medium", "high"]:
    """Confidence tier based on data quantity + validation score.

    Thresholds:
    - high:   days >= 15 AND score >= 0.8
    - medium: days >= 8  AND score >= 0.7 (but not high)
    - low:    otherwise
    """
    if days < 8 or score < 0.7:
        return "low"
    if days >= 15 and score >= 0.8:
        return "high"
    return "medium"


def validate_on_heldout(
    sweep_result: SweepResult,
    heldout_days: list[str],
    live_fills: dict[str, DailyFillSummary],
    run_replay: Callable[[QueueModelCandidate, str], DailyFillSummary],
) -> CalibrationScore:
    """Re-run best candidate on held-out days. Return validation score."""
    live = [live_fills[day] for day in heldout_days if day in live_fills]
    sim = [run_replay(sweep_result.best_candidate, day) for day in heldout_days if day in live_fills]
    return compute_score(sim, live)

"""Exponent grid sweep engine.

Takes a list of QueueModelCandidate and calibration days, runs hftbacktest
replay per (candidate, day), compares simulated fills to live fills,
and selects the highest-scoring candidate.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from research.calibration.scoring import (
    CalibrationScore,
    DailyFillSummary,
    compute_score,
)


@dataclass(frozen=True)
class QueueModelCandidate:
    """One candidate queue model configuration."""

    queue_model: str         # "power_prob", "power_prob2", "power_prob3", "log_prob"
    exponent: float | None   # None for log_prob

    def label(self) -> str:
        if self.exponent is None:
            return self.queue_model
        return f"{self.queue_model}({self.exponent})"


@dataclass(frozen=True)
class SweepResult:
    """Result of an exponent sweep for one instrument."""

    instrument: str
    best_candidate: QueueModelCandidate
    best_score: CalibrationScore
    all_results: tuple[tuple[QueueModelCandidate, CalibrationScore], ...] = ()


def generate_candidates(
    queue_models: list[str],
    exponent_min: float,
    exponent_max: float,
    exponent_step: float,
) -> list[QueueModelCandidate]:
    """Build the grid of candidates to evaluate."""
    if exponent_step <= 0:
        raise ValueError(f"exponent_step must be positive, got {exponent_step}")

    candidates: list[QueueModelCandidate] = []
    for queue_model in queue_models:
        if queue_model.startswith("power_prob"):
            n_steps = round((exponent_max - exponent_min) / exponent_step) + 1
            for i in range(n_steps):
                exponent = round(exponent_min + i * exponent_step, 4)
                candidates.append(QueueModelCandidate(queue_model, exponent))
        else:
            candidates.append(QueueModelCandidate(queue_model, None))
    return candidates


def sweep_exponent(
    instrument: str,
    candidates: list[QueueModelCandidate],
    calibration_days: list[str],
    live_fills: dict[str, DailyFillSummary],
    run_replay: Callable[[QueueModelCandidate, str], DailyFillSummary],
) -> SweepResult:
    """Sweep candidates against live fills. Returns best candidate.

    Args:
        instrument: instrument name
        candidates: queue model candidates to try
        calibration_days: days to use for scoring (training set)
        live_fills: dict date -> DailyFillSummary (live ground truth)
        run_replay: function (candidate, date) -> simulated DailyFillSummary
    """
    all_results: list[tuple[QueueModelCandidate, CalibrationScore]] = []
    live_days = [live_fills[day] for day in calibration_days if day in live_fills]

    for cand in candidates:
        sim_days = [run_replay(cand, day) for day in calibration_days if day in live_fills]
        score = compute_score(sim_days, live_days)
        all_results.append((cand, score))

    if not all_results:
        raise ValueError(
            f"No calibration results for instrument '{instrument}': "
            "candidates list is empty or all calibration_days are absent from live_fills."
        )
    best_cand, best_score = max(all_results, key=lambda x: x[1].composite())
    return SweepResult(
        instrument=instrument,
        best_candidate=best_cand,
        best_score=best_score,
        all_results=tuple(all_results),
    )

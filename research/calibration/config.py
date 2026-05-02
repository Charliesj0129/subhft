"""Calibration profile load/save.

Profiles are stored in config/research/calibration_profiles.yaml.
Each instrument has one entry with calibrated params + validation scores.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

from research.calibration.scoring import CalibrationScore

DEFAULT_PROFILES_PATH = Path("config/research/calibration_profiles.yaml")
"""Canonical location for per-instrument calibration profiles."""


class CalibrationNotFoundError(KeyError):
    """Raised when an instrument has no calibration profile."""


@dataclass(frozen=True)
class CalibrationProfile:
    """Calibrated queue model parameters for one instrument."""

    instrument: str
    queue_model: str
    exponent: float | None
    calibration_date: str
    data_days_used: int
    held_out_days: int
    composite_score: float
    validation_scores: CalibrationScore
    confidence: Literal["low", "medium", "high"]
    expected_fill_rate_per_day: float


def save_calibration_profile(profile: CalibrationProfile, path: Path = DEFAULT_PROFILES_PATH) -> None:
    # Non-atomic read-modify-write: safe for single-process research CLI invocation only.
    # Concurrent calls from multiple processes could race and discard one profile.
    path = Path(path)
    existing: dict = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text()) or {}

    existing[profile.instrument] = {
        "queue_model": profile.queue_model,
        "exponent": profile.exponent,
        "calibration_date": profile.calibration_date,
        "data_days_used": profile.data_days_used,
        "held_out_days": profile.held_out_days,
        "composite_score": profile.composite_score,
        "validation_scores": asdict(profile.validation_scores),
        "confidence": profile.confidence,
        "expected_fill_rate_per_day": profile.expected_fill_rate_per_day,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False))


def load_calibration_profile(instrument: str, path: Path = DEFAULT_PROFILES_PATH) -> CalibrationProfile:
    path = Path(path)
    if not path.exists():
        raise CalibrationNotFoundError(f"No calibration file at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if instrument not in data:
        raise CalibrationNotFoundError(
            f"No calibration profile for {instrument} in {path}. "
            f"Run: uv run python -m research.calibration.cli calibrate --instrument {instrument}"
        )
    entry = data[instrument]
    try:
        vs = entry["validation_scores"]
        return CalibrationProfile(
            instrument=instrument,
            queue_model=entry["queue_model"],
            exponent=entry.get("exponent"),
            calibration_date=entry["calibration_date"],
            data_days_used=entry["data_days_used"],
            held_out_days=entry["held_out_days"],
            composite_score=entry["composite_score"],
            validation_scores=CalibrationScore(
                fill_rate_score=vs["fill_rate_score"],
                adverse_fill_score=vs["adverse_fill_score"],
                pnl_direction_score=vs["pnl_direction_score"],
                pnl_magnitude_score=vs["pnl_magnitude_score"],
            ),
            confidence=entry["confidence"],
            expected_fill_rate_per_day=entry["expected_fill_rate_per_day"],
        )
    except KeyError as exc:
        raise CalibrationNotFoundError(
            f"Calibration profile for {instrument} in {path} is missing required field: {exc}. "
            "Profile may be corrupt or from an incompatible schema version."
        ) from exc

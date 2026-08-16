"""Read-only access to research-generated queue calibration profiles.

The research calibration workflow owns profile generation and persistence.
Platform backtests consume a deliberately small read model from the resulting
YAML artifact so they do not import research implementation modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILES_PATH = Path("config/research/calibration_profiles.yaml")
"""Canonical location for per-instrument calibration profiles."""


class CalibrationNotFoundError(KeyError):
    """Raised when a calibration artifact cannot provide a usable profile."""


@dataclass(frozen=True)
class QueueCalibrationProfile:
    """Queue-model fields consumed by the platform backtest adapter."""

    instrument: str
    queue_model: str
    exponent: float | None
    calibration_date: str


def load_calibration_profile_entry(
    instrument: str,
    path: Path = DEFAULT_PROFILES_PATH,
) -> Mapping[str, Any]:
    """Load one raw profile entry from the shared calibration artifact."""

    path = Path(path)
    if not path.exists():
        raise CalibrationNotFoundError(f"No calibration file at {path}")

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, Mapping):
        raise CalibrationNotFoundError(
            f"Calibration file at {path} must contain an instrument mapping. "
            "Profile may be corrupt or from an incompatible schema version."
        )
    if instrument not in data:
        raise CalibrationNotFoundError(
            f"No calibration profile for {instrument} in {path}. "
            f"Run: uv run python -m research.calibration.cli calibrate --instrument {instrument}"
        )

    entry = data[instrument]
    if not isinstance(entry, Mapping):
        raise CalibrationNotFoundError(
            f"Calibration profile for {instrument} in {path} must be a mapping. "
            "Profile may be corrupt or from an incompatible schema version."
        )
    return entry


def load_queue_calibration_profile(
    instrument: str,
    path: Path = DEFAULT_PROFILES_PATH,
) -> QueueCalibrationProfile:
    """Load the queue-model read view while validating the complete artifact schema."""

    path = Path(path)
    entry = load_calibration_profile_entry(instrument, path)
    try:
        validation_scores = entry["validation_scores"]
        queue_model = entry["queue_model"]
        exponent = entry.get("exponent")
        calibration_date = entry["calibration_date"]

        # Preserve the adapter's previous fail-closed behavior: profiles missing
        # research evidence fields are incompatible even though this read model
        # consumes only the queue-model fields above.
        entry["data_days_used"]
        entry["held_out_days"]
        entry["composite_score"]
        if not isinstance(validation_scores, Mapping):
            raise KeyError("validation_scores")
        validation_scores["fill_rate_score"]
        validation_scores["adverse_fill_score"]
        validation_scores["pnl_direction_score"]
        validation_scores["pnl_magnitude_score"]
        entry["confidence"]
        entry["expected_fill_rate_per_day"]
    except KeyError as exc:
        raise CalibrationNotFoundError(
            f"Calibration profile for {instrument} in {path} is missing required field: {exc}. "
            "Profile may be corrupt or from an incompatible schema version."
        ) from exc

    return QueueCalibrationProfile(
        instrument=instrument,
        queue_model=queue_model,
        exponent=exponent,
        calibration_date=calibration_date,
    )


__all__ = [
    "CalibrationNotFoundError",
    "DEFAULT_PROFILES_PATH",
    "QueueCalibrationProfile",
    "load_calibration_profile_entry",
    "load_queue_calibration_profile",
]

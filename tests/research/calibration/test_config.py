import pytest
import yaml

from research.calibration.config import (
    CalibrationNotFoundError,
    CalibrationProfile,
    load_calibration_profile,
    save_calibration_profile,
)
from research.calibration.scoring import CalibrationScore


def test_save_and_load_profile(tmp_path):
    path = tmp_path / "profiles.yaml"
    profile = CalibrationProfile(
        instrument="TMFD6",
        queue_model="power_prob",
        exponent=1.5,
        calibration_date="2026-04-20",
        data_days_used=12,
        held_out_days=5,
        composite_score=0.78,
        validation_scores=CalibrationScore(0.82, 0.75, 0.80, 0.65),
        confidence="medium",
        expected_fill_rate_per_day=21.4,
    )
    save_calibration_profile(profile, path)
    loaded = load_calibration_profile("TMFD6", path)
    assert loaded.exponent == 1.5
    assert loaded.confidence == "medium"


def test_load_calibration_profile_missing_raises(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"TMFD6": {
        "queue_model": "power_prob", "exponent": 1.5,
        "calibration_date": "2026-04-20",
        "data_days_used": 12, "held_out_days": 5,
        "composite_score": 0.78,
        "validation_scores": {"fill_rate_score": 0.82, "adverse_fill_score": 0.75,
                               "pnl_direction_score": 0.8, "pnl_magnitude_score": 0.65},
        "confidence": "medium", "expected_fill_rate_per_day": 21.4,
    }}))
    with pytest.raises(CalibrationNotFoundError):
        load_calibration_profile("TXFD6", path)


def test_profile_provenance_round_trips_and_a_pre_c4_entry_has_none(tmp_path):
    path = tmp_path / "profiles.yaml"
    base = dict(
        queue_model="power_prob",
        exponent=1.5,
        calibration_date="2026-04-20",
        data_days_used=12,
        held_out_days=5,
        composite_score=0.78,
        validation_scores=CalibrationScore(0.82, 0.75, 0.80, 0.65),
        confidence="medium",
        expected_fill_rate_per_day=21.4,
    )
    provenance = {"run_stamp": {"git_commit": "abc"}, "train_days": ["2026-04-01"], "sweep_trials": 44}
    save_calibration_profile(CalibrationProfile(instrument="TMFD6", provenance=provenance, **base), path)
    save_calibration_profile(CalibrationProfile(instrument="TXFD6", **base), path)

    assert load_calibration_profile("TMFD6", path).provenance == provenance
    assert load_calibration_profile("TXFD6", path).provenance is None
    assert "provenance" not in yaml.safe_load(path.read_text())["TXFD6"]

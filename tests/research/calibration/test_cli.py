"""Tests for research.calibration.cli.

Focuses on the fallback paths (data gap, missing audit, stub blocked) since the
full sweep path requires ClickHouse + hftbacktest integration (covered by A8+).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from research.calibration import cli
from research.calibration.config import load_calibration_profile


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        instrument="TMFD6",
        audit_report=Path("/nonexistent/audit.json"),
        l2_data_dir=Path("/nonexistent/l2"),
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub=False,
        output=Path("/tmp/test_profile.yaml"),
        artifacts_dir=Path("/tmp/test_artifacts"),
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_calibrate_missing_audit_writes_fallback(tmp_path, capsys):
    output = tmp_path / "profiles.yaml"
    result = cli.cmd_calibrate(
        _make_args(
            audit_report=tmp_path / "missing.json",
            output=output,
            artifacts_dir=tmp_path / "artifacts",
        )
    )
    assert result == 0
    profile = load_calibration_profile("TMFD6", output)
    assert profile.exponent == cli.LITERATURE_DEFAULT_EXPONENT
    assert profile.confidence == "low"
    assert profile.composite_score == 0.0
    assert profile.data_days_used == 0


def test_calibrate_instrument_not_in_audit_writes_fallback(tmp_path):
    # Create audit with only TXFD6
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "per_instrument": {
            "TXFD6": {"usable_calibration_days": ["2026-03-01"]},
        },
        "summary": {},
    }))
    output = tmp_path / "profiles.yaml"
    result = cli.cmd_calibrate(
        _make_args(
            instrument="TMFD6",  # not in audit
            audit_report=audit_path,
            output=output,
            artifacts_dir=tmp_path / "artifacts",
        )
    )
    assert result == 0
    profile = load_calibration_profile("TMFD6", output)
    assert profile.confidence == "low"


def test_calibrate_few_days_writes_fallback(tmp_path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "per_instrument": {
            "TMFD6": {"usable_calibration_days": ["2026-03-01", "2026-03-02"]},
        },
        "summary": {},
    }))
    output = tmp_path / "profiles.yaml"
    result = cli.cmd_calibrate(
        _make_args(
            audit_report=audit_path,
            output=output,
            artifacts_dir=tmp_path / "artifacts",
        )
    )
    assert result == 0
    profile = load_calibration_profile("TMFD6", output)
    assert profile.confidence == "low"


def test_calibrate_allow_stub_emits_warning(tmp_path, capsys):
    result = cli.cmd_calibrate(
        _make_args(
            audit_report=tmp_path / "missing.json",
            output=tmp_path / "profiles.yaml",
            artifacts_dir=tmp_path / "artifacts",
            allow_stub=True,
        )
    )
    captured = capsys.readouterr()
    assert "--allow-stub enabled" in captured.err
    assert "stub mode" in captured.err.lower()


def test_write_fallback_profile_structure(tmp_path):
    output = tmp_path / "profiles.yaml"
    cli._write_fallback_profile("TMFD6", output, "test reason")

    loaded = yaml.safe_load(output.read_text())
    assert "TMFD6" in loaded
    entry = loaded["TMFD6"]
    assert entry["exponent"] == cli.LITERATURE_DEFAULT_EXPONENT
    assert entry["queue_model"] == "power_prob"
    assert entry["confidence"] == "low"
    assert entry["composite_score"] == 0.0
    assert entry["data_days_used"] == 0
    assert entry["validation_scores"] == {
        "fill_rate_score": 0.0,
        "adverse_fill_score": 0.0,
        "pnl_direction_score": 0.0,
        "pnl_magnitude_score": 0.0,
    }


def test_load_live_fills_from_audit_missing_file():
    days, fills = cli._load_live_fills_from_audit(Path("/nonexistent"), "TMFD6")
    assert days == []
    assert fills == {}


def test_load_live_fills_from_audit_instrument_present(tmp_path):
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "per_instrument": {
            "TMFD6": {"usable_calibration_days": ["2026-03-01", "2026-03-02"]},
        },
        "summary": {},
    }))
    days, fills = cli._load_live_fills_from_audit(audit_path, "TMFD6")
    assert days == ["2026-03-01", "2026-03-02"]
    assert fills == {}  # placeholder — aggregation deferred

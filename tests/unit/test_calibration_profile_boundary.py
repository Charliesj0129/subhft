"""Regression checks for the platform/research calibration artifact boundary."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import hft_platform.config.calibration_profiles as artifact
import research.calibration.config as research_config
from research.calibration.scoring import CalibrationScore


def _research_imports(source: str) -> list[str]:
    imports: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name for alias in node.names if alias.name == "research" or alias.name.startswith("research.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "research" or module.startswith("research."):
                imports.append(module)
    return imports


def _write_research_profile(path: Path) -> None:
    research_config.save_calibration_profile(
        research_config.CalibrationProfile(
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
        ),
        path,
    )


def test_research_config_reexports_platform_artifact_identity() -> None:
    assert research_config.CalibrationNotFoundError is artifact.CalibrationNotFoundError
    assert research_config.DEFAULT_PROFILES_PATH is artifact.DEFAULT_PROFILES_PATH


def test_queue_profile_reader_consumes_research_writer_artifact(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    _write_research_profile(path)

    profile = artifact.load_queue_calibration_profile("TMFD6", path)

    assert profile == artifact.QueueCalibrationProfile(
        instrument="TMFD6",
        queue_model="power_prob",
        exponent=1.5,
        calibration_date="2026-04-20",
    )


def test_queue_profile_reader_rejects_missing_research_evidence_field(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    _write_research_profile(path)
    payload = yaml.safe_load(path.read_text())
    del payload["TMFD6"]["validation_scores"]["pnl_magnitude_score"]
    path.write_text(yaml.safe_dump(payload))

    with pytest.raises(
        artifact.CalibrationNotFoundError,
        match="missing required field.*pnl_magnitude_score",
    ):
        artifact.load_queue_calibration_profile("TMFD6", path)


def test_profile_entry_reader_rejects_missing_artifact(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(artifact.CalibrationNotFoundError, match="No calibration file"):
        artifact.load_calibration_profile_entry("TMFD6", path)


def test_profile_entry_reader_rejects_non_mapping_document(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump(["not", "a", "mapping"]))

    with pytest.raises(artifact.CalibrationNotFoundError, match="must contain an instrument mapping"):
        artifact.load_calibration_profile_entry("TMFD6", path)


def test_profile_entry_reader_rejects_non_mapping_instrument_entry(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"TMFD6": "not-a-mapping"}))

    with pytest.raises(artifact.CalibrationNotFoundError, match="must be a mapping"):
        artifact.load_calibration_profile_entry("TMFD6", path)


def test_queue_profile_reader_rejects_non_mapping_validation_scores(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    _write_research_profile(path)
    payload = yaml.safe_load(path.read_text())
    payload["TMFD6"]["validation_scores"] = []
    path.write_text(yaml.safe_dump(payload))

    with pytest.raises(
        artifact.CalibrationNotFoundError,
        match="missing required field.*validation_scores",
    ):
        artifact.load_queue_calibration_profile("TMFD6", path)


def test_platform_and_research_readers_share_missing_profile_error(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    _write_research_profile(path)

    for loader in (
        artifact.load_queue_calibration_profile,
        research_config.load_calibration_profile,
    ):
        with pytest.raises(artifact.CalibrationNotFoundError, match="No calibration profile for TXFD6"):
            loader("TXFD6", path)


def test_backtest_adapter_source_has_no_research_import() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/hft_platform/backtest/adapter.py").read_text(encoding="utf-8")

    assert _research_imports(source) == []


def test_queue_model_auto_does_not_load_research_package() -> None:
    code = """
import sys
import tempfile
from pathlib import Path

import numpy as np

from hft_platform.backtest.adapter import HftBacktestAdapter
from hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    _event_dtype,
)
from hft_platform.strategy.base import BaseStrategy


class NullStrategy(BaseStrategy):
    def handle_event(self, ctx, event):
        return []


events = np.array(
    [
        (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT, 1_000_000_000, 1_001_000_000, 17000.0, 5, 0, 0, 0.0),
        (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT, 1_000_000_000, 1_001_000_000, 17001.0, 3, 0, 0, 0.0),
        (TRADE_EVENT | EXCH_EVENT | BUY_EVENT, 2_000_000_000, 2_001_000_000, 17000.5, 1, 0, 0, 0.0),
    ],
    dtype=_event_dtype(),
)
profile_yaml = '''\
TMFD6:
  queue_model: power_prob
  exponent: 1.5
  calibration_date: '2026-04-20'
  data_days_used: 12
  held_out_days: 5
  composite_score: 0.78
  validation_scores:
    fill_rate_score: 0.82
    adverse_fill_score: 0.75
    pnl_direction_score: 0.80
    pnl_magnitude_score: 0.65
  confidence: medium
  expected_fill_rate_per_day: 21.4
'''
with tempfile.TemporaryDirectory() as tmpdir:
    profile_path = Path(tmpdir) / "profiles.yaml"
    profile_path.write_text(profile_yaml)
    adapter = HftBacktestAdapter(
        strategy=NullStrategy(strategy_id="test"),
        asset_symbol="TMFD6",
        data=events,
        tick_size=1.0,
        lot_size=1.0,
        queue_model="auto",
        instrument="TMFD6",
        calibration_profile_path=str(profile_path),
    )

loaded = sorted(
    module
    for module in sys.modules
    if module == "research" or module.startswith("research.")
)
print(f"RESULT|{adapter.queue_model}|{adapter.calibration_profile_id}|{','.join(loaded)}")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("RESULT|PowerProbQueueModel(1.5)|TMFD6_2026-04-20|")


def test_boundary_scanner_detects_forbidden_research_import() -> None:
    assert _research_imports("from research.calibration.config import load_calibration_profile") == [
        "research.calibration.config"
    ]

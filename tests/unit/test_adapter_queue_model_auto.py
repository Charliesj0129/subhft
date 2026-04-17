"""Tests for HftBacktestAdapter queue_model='auto' + calibration profile loading."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("hftbacktest")

from hft_platform.backtest.adapter import HFTBACKTEST_AVAILABLE, HftBacktestAdapter
from hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_EVENT,
    EXCH_EVENT,
    SELL_EVENT,
    TRADE_EVENT,
    _event_dtype,
)
from research.calibration.config import (
    CalibrationProfile,
    save_calibration_profile,
)
from research.calibration.scoring import CalibrationScore


def _minimal_events() -> np.ndarray:
    dtype = _event_dtype()
    return np.array(
        [
            (DEPTH_EVENT | EXCH_EVENT | BUY_EVENT,
             1_000_000_000, 1_001_000_000, 17000.0, 5, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | SELL_EVENT,
             1_000_000_000, 1_001_000_000, 17001.0, 3, 0, 0, 0.0),
            (TRADE_EVENT | EXCH_EVENT | BUY_EVENT,
             2_000_000_000, 2_001_000_000, 17000.5, 1, 0, 0, 0.0),
        ],
        dtype=dtype,
    )


def _null_strategy_class():
    from hft_platform.strategy.base import BaseStrategy

    class NullStrategy(BaseStrategy):
        def handle_event(self, ctx, event):
            return []

    return NullStrategy


def _write_profile(path: Path, instrument: str, queue_model: str, exponent: float | None):
    save_calibration_profile(
        CalibrationProfile(
            instrument=instrument,
            queue_model=queue_model,
            exponent=exponent,
            calibration_date="2026-04-20",
            data_days_used=12,
            held_out_days=5,
            composite_score=0.78,
            validation_scores=CalibrationScore(0.8, 0.75, 0.8, 0.65),
            confidence="medium",
            expected_fill_rate_per_day=21.4,
        ),
        path,
    )


@pytest.mark.skipif(not HFTBACKTEST_AVAILABLE, reason="hftbacktest not installed")
def test_adapter_queue_model_auto_requires_instrument(tmp_path):
    Strategy = _null_strategy_class()
    with pytest.raises(ValueError, match="instrument required"):
        HftBacktestAdapter(
            strategy=Strategy(strategy_id="test"),
            asset_symbol="TMFD6",
            data=_minimal_events(),
            tick_size=1.0, lot_size=1.0,
            queue_model="auto",
        )


@pytest.mark.skipif(not HFTBACKTEST_AVAILABLE, reason="hftbacktest not installed")
def test_adapter_queue_model_auto_loads_profile(tmp_path):
    profile_path = tmp_path / "profiles.yaml"
    _write_profile(profile_path, "TMFD6", "power_prob", 1.5)

    Strategy = _null_strategy_class()
    adapter = HftBacktestAdapter(
        strategy=Strategy(strategy_id="test"),
        asset_symbol="TMFD6",
        data=_minimal_events(),
        tick_size=1.0, lot_size=1.0,
        queue_model="auto",
        instrument="TMFD6",
        calibration_profile_path=profile_path,
    )
    # Adapter must expose the resolved queue model name AND calibration profile id
    assert "PowerProbQueueModel" in adapter.queue_model
    assert "1.5" in adapter.queue_model
    assert adapter.calibration_profile_id == "TMFD6_2026-04-20"


@pytest.mark.skipif(not HFTBACKTEST_AVAILABLE, reason="hftbacktest not installed")
def test_adapter_queue_model_auto_supports_log_prob(tmp_path):
    profile_path = tmp_path / "profiles.yaml"
    _write_profile(profile_path, "TMFD6", "log_prob", None)

    Strategy = _null_strategy_class()
    adapter = HftBacktestAdapter(
        strategy=Strategy(strategy_id="test"),
        asset_symbol="TMFD6",
        data=_minimal_events(),
        tick_size=1.0, lot_size=1.0,
        queue_model="auto",
        instrument="TMFD6",
        calibration_profile_path=profile_path,
    )
    assert adapter.queue_model == "LogProbQueueModel"


@pytest.mark.skipif(not HFTBACKTEST_AVAILABLE, reason="hftbacktest not installed")
def test_adapter_queue_model_explicit_still_works(tmp_path):
    Strategy = _null_strategy_class()
    adapter = HftBacktestAdapter(
        strategy=Strategy(strategy_id="test"),
        asset_symbol="TMFD6",
        data=_minimal_events(),
        tick_size=1.0, lot_size=1.0,
        queue_model="PowerProbQueueModel(2.0)",
    )
    assert adapter.queue_model == "PowerProbQueueModel(2.0)"
    assert adapter.calibration_profile_id == "uncalibrated"

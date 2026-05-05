"""End-to-end Slice A integration tests.

Three scenarios:
1. R47-OE1 fingerprint payload + strict profile -> Gate C KILL.
2. Robust payload + strict profile -> Gate C PASS.
3. Same payload as (1) without profile -> behavior identical to pre-change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile


@dataclass
class _ParityReport:
    """Synthetic ReplayParityReport stand-in.

    Real reports are wired by Slice C Task 11 (BacktestResult.replay_parity_report)
    and consumed by ReplayParityGate. The strict profile started enforcing parity
    in Task 9, so these tests inject a passing report directly into the payload.
    """

    match_pct: float
    first_divergence_idx: int | None = None


@pytest.fixture(scope="module")
def strict_profile() -> Any:
    return load_profile("config/research/profiles/vm_ul6_strict.yaml")


def _r47_payload() -> dict:
    return {
        "run_id": "test_r47",
        "config_hash": "abc",
        "instrument": "TMFD6",
        "strategy_name": "r47_maker_tmf",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": 2253.0,
        "n_fills": 39,
        "n_trading_days": 31,
        "equity_curve": None,
        "pnl_per_fill": 61.5,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 1.26,
        "daily_pnl": [2325.0] + [-2.4] * 30,
        # Slice C: passing parity report so the R47 KILL is attributable to
        # the small-sample/single-day-dominance gates, not to missing parity.
        "replay_parity_report": _ParityReport(match_pct=99.0),
    }


def _robust_payload() -> dict:
    rng = np.random.default_rng(0)
    daily = rng.normal(loc=20.0, scale=10.0, size=60).tolist()
    return {
        "run_id": "test_robust",
        "config_hash": "xyz",
        "instrument": "TXFD6",
        "strategy_name": "synthetic_robust",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": float(sum(daily)),
        "n_fills": 360,
        "n_trading_days": 60,
        "equity_curve": None,
        "pnl_per_fill": float(sum(daily)) / 360.0,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 6.0,
        "daily_pnl": daily,
        # Slice C: passing parity report — robust path must clear the new
        # strict replay_parity sub-gate.
        "replay_parity_report": _ParityReport(match_pct=98.5),
    }


class TestStrictProfileEndToEnd:
    def test_r47_pattern_kills_under_strict_profile(self, strict_profile: Any) -> None:
        thresholds = strict_profile.thresholds_for(strategy_type="maker")
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_r47_payload(),
            thresholds=thresholds,
            profile=strict_profile,
        )
        assert blocking is not None
        assert blocking["passed"] is False, blocking
        failing = {f["name"] for f in blocking["failing"]}
        assert "min_sample_size" in failing
        assert "single_day_dominance" in failing
        assert "loo_day_sensitivity" in failing

    def test_robust_pattern_passes_under_strict_profile(self, strict_profile: Any) -> None:
        thresholds = strict_profile.thresholds_for(strategy_type="maker")
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_robust_payload(),
            thresholds=thresholds,
            profile=strict_profile,
        )
        assert blocking is not None
        assert blocking["passed"] is True, blocking["failing"]

    def test_loose_profile_preserves_advisory_only_behavior(self) -> None:
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_r47_payload(),
            thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55},
            profile=None,
        )
        assert blocking is None
        assert any(g["name"] == "fill_quality" for g in advisory)

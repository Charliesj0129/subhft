"""Tests for DayLevelBootstrapCIGate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.day_bootstrap_ci import DayLevelBootstrapCIGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestDayLevelBootstrapCIGate:
    def test_passes_for_clearly_positive_daily_pnl(self) -> None:
        gate = DayLevelBootstrapCIGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=10.0, scale=1.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "bootstrap_ci_lower_bound_min": 0.0,
                "bootstrap_n_resamples": 1000,
                "bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is True
        assert out.metrics["ci_lower"] > 0.0

    def test_fails_for_zero_mean_noise(self) -> None:
        gate = DayLevelBootstrapCIGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=0.0, scale=10.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "bootstrap_ci_lower_bound_min": 0.0,
                "bootstrap_n_resamples": 1000,
                "bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is False

    def test_fails_when_too_few_days(self) -> None:
        gate = DayLevelBootstrapCIGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"bootstrap_ci_lower_bound_min": 0.0})
        assert out.passed is False
        assert "insufficient" in out.details

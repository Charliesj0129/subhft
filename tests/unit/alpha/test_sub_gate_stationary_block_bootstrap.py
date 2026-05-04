"""Tests for StationaryBlockBootstrapGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.stationary_block_bootstrap import (
    StationaryBlockBootstrapGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestStationaryBlockBootstrapGate:
    def test_passes_for_strong_signal(self) -> None:
        gate = StationaryBlockBootstrapGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=20.0, scale=1.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
                "block_bootstrap_n_resamples": 500,
                "block_bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is True

    def test_fails_for_zero_mean_noise(self) -> None:
        gate = StationaryBlockBootstrapGate()
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=0.0, scale=10.0, size=100)).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
                "block_bootstrap_n_resamples": 500,
                "block_bootstrap_alpha": 0.05,
            },
        )
        assert out.passed is False

    def test_fails_when_input_shorter_than_block_size(self) -> None:
        gate = StationaryBlockBootstrapGate()
        r = _FakeResult(daily_pnl=[1.0, 2.0])
        out = gate.evaluate(
            r,
            config=None,
            thresholds={
                "block_bootstrap_ci_lower_bound_min": 0.0,
                "block_bootstrap_block_size_days": 5,
            },
        )
        assert out.passed is False
        assert "block_size" in out.details

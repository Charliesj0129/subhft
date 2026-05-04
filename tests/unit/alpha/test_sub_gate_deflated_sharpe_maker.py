"""Tests for DeflatedSharpeForMakerGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.deflated_sharpe_maker import (
    DeflatedSharpeForMakerGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestDeflatedSharpeForMakerGate:
    def test_applies_only_to_maker(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        assert gate.applies_to == {"maker"}

    def test_passes_for_strong_sharpe(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        rng = np.random.default_rng(0)
        daily = rng.normal(loc=2.0, scale=1.0, size=200).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={"deflated_sharpe_min": 0.5, "deflated_n_trials": 1},
        )
        assert out.passed is True

    def test_fails_for_thin_sharpe_with_many_trials(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        rng = np.random.default_rng(0)
        daily = rng.normal(loc=0.05, scale=1.0, size=30).tolist()
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(
            r,
            config=None,
            thresholds={"deflated_sharpe_min": 0.5, "deflated_n_trials": 100},
        )
        assert out.passed is False

    def test_insufficient_days_fails(self) -> None:
        gate = DeflatedSharpeForMakerGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"deflated_sharpe_min": 0.5})
        assert out.passed is False

"""Tests for DeflatedSharpeForMakerGate."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hft_platform.alpha._sub_gates.deflated_sharpe_maker import (
    DeflatedSharpeForMakerGate,
)
from hft_platform.alpha.stats import selection_sr0


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


class TestMakerDeflationIsSr0:
    """The deduction is ``stats.selection_sr0``; the 0.5 Sharpe-unit threshold is unchanged."""

    _DAILY = np.random.default_rng(3).normal(loc=0.3, scale=1.0, size=120).tolist()

    def _metrics(self, n_trials: int) -> dict:
        out = DeflatedSharpeForMakerGate().evaluate(
            _FakeResult(daily_pnl=self._DAILY),
            config=None,
            thresholds={"deflated_sharpe_min": 0.5, "deflated_n_trials": n_trials},
        )
        return out.metrics

    def test_one_trial_deflates_nothing(self) -> None:
        metrics = self._metrics(1)
        assert metrics["penalty"] == 0.0
        assert metrics["deflated_sharpe"] == metrics["sharpe"]

    def test_many_trials_deduct_sr0_in_annualized_units(self) -> None:
        metrics = self._metrics(100)
        expected = selection_sr0(100, observations=120, annualization=252.0)
        assert metrics["penalty"] == expected
        assert metrics["deflated_sharpe"] == metrics["sharpe"] - expected

    def test_many_trials_never_deduct_less_than_the_retired_penalty(self) -> None:
        retired = math.sqrt(2.0 * math.log(100) / 120)
        assert self._metrics(100)["penalty"] >= retired

    def test_reports_the_dsr_probability(self) -> None:
        one, many = self._metrics(1)["dsr_probability"], self._metrics(100)["dsr_probability"]
        assert 0.0 <= many < one <= 1.0

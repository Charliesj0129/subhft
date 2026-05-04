"""Tests for LOODaySensitivityGate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.loo_day_sensitivity import LOODaySensitivityGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestLOODaySensitivityGate:
    def test_passes_when_sign_is_robust(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[10.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is True

    def test_fails_when_dropping_top_day_flips_sign(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[100.0] + [-2.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is False
        assert out.metrics["worst_loo_pnl"] < 0.0

    def test_disabled_threshold_passes(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[100.0] + [-2.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": False})
        assert out.passed is True

    def test_insufficient_days_fails_with_explicit_detail(self) -> None:
        gate = LOODaySensitivityGate()
        r = _FakeResult(daily_pnl=[1.0])
        out = gate.evaluate(r, config=None, thresholds={"loo_day_sign_preserved": True})
        assert out.passed is False
        assert "insufficient days" in out.details

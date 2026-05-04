"""Tests for MinSampleSizeGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.min_sample_size import MinSampleSizeGate


@dataclass
class _FakeResult:
    n_fills: int = 0
    n_trading_days: int = 0
    daily_pnl: list[Any] = field(default_factory=list)


class TestMinSampleSizeGate:
    def test_passes_when_both_above_threshold(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=400, n_trading_days=70)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is True
        assert out.metrics["n_fills"] == 400.0

    def test_fails_when_fills_below(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=39, n_trading_days=70)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is False
        assert "39" in out.details

    def test_fails_when_days_below(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=400, n_trading_days=31)
        out = gate.evaluate(r, config=None, thresholds={"min_fills": 300, "min_days": 60})
        assert out.passed is False
        assert "31" in out.details

    def test_uses_defaults_when_thresholds_absent(self) -> None:
        gate = MinSampleSizeGate()
        r = _FakeResult(n_fills=10, n_trading_days=2)
        out = gate.evaluate(r, config=None, thresholds={})
        assert out.passed is True

    def test_applies_to_includes_maker_and_taker(self) -> None:
        gate = MinSampleSizeGate()
        assert "maker" in gate.applies_to
        assert "taker" in gate.applies_to

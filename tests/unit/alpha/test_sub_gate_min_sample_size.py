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


class TestSampleAdequacyLabel:
    """Goal §4: sub-threshold runs must be labelled, not silently failed.

    Label routes triage:
      - adequate            : both fractions >= 1.0 (gate passes)
      - promising           : min fraction in [0.5, 1.0) (close, retry later)
      - needs_more_sample   : min fraction in (0.0, 0.5)
      - inconclusive        : zero activity on either axis
    """

    def _gate(self) -> MinSampleSizeGate:
        return MinSampleSizeGate()

    def test_label_adequate_when_both_above_threshold(self) -> None:
        out = self._gate().evaluate(
            _FakeResult(n_fills=400, n_trading_days=70),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "adequate"
        assert out.passed is True

    def test_label_promising_when_just_below_threshold(self) -> None:
        # 240/300 = 0.80; 50/60 = 0.83 → min 0.80, in [0.5, 1.0)
        out = self._gate().evaluate(
            _FakeResult(n_fills=240, n_trading_days=50),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "promising"
        assert out.passed is False
        assert out.metrics["fills_frac"] == 240.0 / 300.0
        assert out.metrics["days_frac"] == 50.0 / 60.0

    def test_label_needs_more_sample_when_well_below(self) -> None:
        # 60/300 = 0.20; 20/60 = 0.33 → min 0.20, in (0, 0.5)
        out = self._gate().evaluate(
            _FakeResult(n_fills=60, n_trading_days=20),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "needs_more_sample"
        assert out.passed is False

    def test_label_inconclusive_when_zero_fills(self) -> None:
        out = self._gate().evaluate(
            _FakeResult(n_fills=0, n_trading_days=20),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "inconclusive"
        assert out.passed is False

    def test_label_inconclusive_when_zero_days(self) -> None:
        out = self._gate().evaluate(
            _FakeResult(n_fills=100, n_trading_days=0),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "inconclusive"
        assert out.passed is False

    def test_label_adequate_when_thresholds_zero(self) -> None:
        # When no thresholds configured, any non-negative sample is adequate.
        out = self._gate().evaluate(
            _FakeResult(n_fills=1, n_trading_days=1),
            config=None,
            thresholds={},
        )
        assert out.metrics["sample_adequacy_label"] == "adequate"
        assert out.passed is True

    def test_boundary_at_half_is_promising_not_needs_more(self) -> None:
        # 150/300 = 0.50 exactly → label boundary belongs to "promising"
        # (closed lower bound) so triage isn't penalised at the cliff.
        out = self._gate().evaluate(
            _FakeResult(n_fills=150, n_trading_days=60),
            config=None,
            thresholds={"min_fills": 300, "min_days": 60},
        )
        assert out.metrics["sample_adequacy_label"] == "promising"

"""Tests for ReplayParityGate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hft_platform.alpha._sub_gates.replay_parity import ReplayParityGate


@dataclass
class _FakeReport:
    match_pct: float
    first_divergence_idx: int | None = None


@dataclass
class _FakeStrictReport:
    """v2 report carrying the strict ``ok`` flag + mismatch_type."""

    match_pct: float
    ok: bool
    first_divergence_idx: int | None = None
    mismatch_type: str | None = None


@dataclass
class _FakeResult:
    replay_parity_report: Any = None


class TestReplayParityGate:
    def test_passes_when_match_pct_above_threshold(self) -> None:
        gate = ReplayParityGate()
        report = _FakeReport(match_pct=96.0)
        result = _FakeResult(replay_parity_report=report)
        thresholds = {"replay_parity_match_pct_min": 95.0}

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert out.metrics["match_pct"] == 96.0
        assert out.metrics["threshold"] == 95.0

    def test_fails_when_below_threshold(self) -> None:
        gate = ReplayParityGate()
        report = _FakeReport(match_pct=80.0, first_divergence_idx=12)
        result = _FakeResult(replay_parity_report=report)
        thresholds = {"replay_parity_match_pct_min": 95.0}

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is False
        assert out.metrics["match_pct"] == 80.0
        assert out.metrics["first_divergence_idx"] == 12.0

    def test_missing_report_marks_gate_failed(self) -> None:
        gate = ReplayParityGate()
        result = _FakeResult(replay_parity_report=None)
        thresholds = {"replay_parity_match_pct_min": 95.0}

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is False
        assert "missing" in out.details.lower()

    def test_strict_ok_false_fails_even_with_high_match_pct(self) -> None:
        """Fail-closed: any divergence (ok=False) blocks promotion regardless
        of match_pct — a structural ordering/missing mismatch can leave
        match_pct high yet must not certify parity."""
        gate = ReplayParityGate()
        report = _FakeStrictReport(match_pct=99.9, ok=False, first_divergence_idx=3, mismatch_type="ordering_mismatch")
        result = _FakeResult(replay_parity_report=report)
        thresholds = {"replay_parity_match_pct_min": 95.0}

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is False
        assert out.metrics["ok"] == 0.0
        assert "ordering_mismatch" in out.details

    def test_strict_ok_true_passes(self) -> None:
        gate = ReplayParityGate()
        report = _FakeStrictReport(match_pct=100.0, ok=True)
        result = _FakeResult(replay_parity_report=report)

        out = gate.evaluate(result, config=None, thresholds={})

        assert out.passed is True
        assert out.metrics["ok"] == 1.0

    def test_applies_to_includes_maker_and_taker(self) -> None:
        gate = ReplayParityGate()
        assert "maker" in gate.applies_to
        assert "taker" in gate.applies_to

    def test_name_is_replay_parity(self) -> None:
        gate = ReplayParityGate()
        assert gate.name == "replay_parity"


class TestReplayParityRegistration:
    def test_replay_parity_auto_registered(self) -> None:
        """Slice C task 9: ReplayParityGate must be in the built-in registry
        after ensure_builtin_sub_gates_registered() runs (idempotent)."""
        from hft_platform.alpha._sub_gates import (
            ensure_builtin_sub_gates_registered,
            get_registered_sub_gates,
        )

        ensure_builtin_sub_gates_registered()
        names = {g.name for g in get_registered_sub_gates()}
        assert "replay_parity" in names

"""Tests for research.tools.digital_twin — Paper Trade Digital Twin generator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha.experiments import PaperTradeSession
from research.tools.digital_twin import (
    _model_reject_rate,
    generate_digital_twin_sessions,
)


@dataclass(frozen=True)
class _FakeBacktestResult:
    equity_curve: np.ndarray
    positions: np.ndarray


def _make_result(n: int = 500, seed: int = 42) -> _FakeBacktestResult:
    """Create a synthetic BacktestResult-like object."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0002, 0.01, n)
    equity = np.cumsum(returns) + 1_000_000.0
    positions = np.sign(rng.normal(0, 1, n))
    return _FakeBacktestResult(equity_curve=equity, positions=positions)


class TestGenerateDigitalTwinSessions:
    def test_returns_at_least_5_sessions(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        assert len(sessions) >= 5

    def test_custom_n_sessions(self) -> None:
        result = _make_result(n=1000)
        sessions = generate_digital_twin_sessions(result, "test_alpha", n_sessions=7)
        assert len(sessions) == 7

    def test_n_sessions_minimum_clamped_to_5(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha", n_sessions=2)
        assert len(sessions) >= 5

    def test_at_least_2_distinct_regimes(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        regimes = {s.regime for s in sessions}
        assert len(regimes) >= 2

    def test_regime_diversity_forced_when_uniform(self) -> None:
        """Even with a flat equity curve (0 volatility everywhere), regimes are diverse."""
        equity = np.full(100, 1_000_000.0)
        positions = np.zeros(100)
        result = _FakeBacktestResult(equity_curve=equity, positions=positions)
        sessions = generate_digital_twin_sessions(result, "flat_alpha")
        regimes = {s.regime for s in sessions}
        assert len(regimes) >= 2

    def test_reject_rate_p95_within_bounds(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        for s in sessions:
            assert s.reject_rate_p95 is not None
            assert s.reject_rate_p95 <= 0.01
            assert s.execution_reject_rate <= 0.01

    def test_notes_contain_digital_twin(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        for s in sessions:
            assert "digital_twin" in s.notes

    def test_all_fields_populated(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        for s in sessions:
            assert isinstance(s, PaperTradeSession)
            assert s.alpha_id == "test_alpha"
            assert len(s.session_id) > 0
            assert len(s.started_at) > 0
            assert len(s.ended_at) > 0
            assert s.duration_seconds > 0
            assert len(s.trading_day) > 0
            assert s.fills >= 1
            assert s.drift_alerts == 0
            assert s.session_duration_minutes is not None
            assert s.session_duration_minutes > 0
            assert s.regime in ("trending", "mean_reverting")

    def test_drift_alerts_zero(self) -> None:
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")
        for s in sessions:
            assert s.drift_alerts == 0

    def test_with_latency_profile(self) -> None:
        result = _make_result()
        profile: dict[str, Any] = {
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
            "local_decision_pipeline_latency_us": 250,
        }
        sessions = generate_digital_twin_sessions(
            result, "test_alpha", latency_profile=profile
        )
        assert len(sessions) >= 5
        for s in sessions:
            assert s.reject_rate_p95 is not None
            assert s.reject_rate_p95 <= 0.01

    def test_equity_too_short_raises(self) -> None:
        equity = np.array([1.0, 2.0])
        positions = np.array([0.0, 1.0])
        result = _FakeBacktestResult(equity_curve=equity, positions=positions)
        with pytest.raises(ValueError, match="equity_curve has 2 points"):
            generate_digital_twin_sessions(result, "short_alpha", n_sessions=5)

    def test_sessions_pass_gate_e_basic_checks(self) -> None:
        """Verify the sessions would pass the basic Gate E checks."""
        result = _make_result()
        sessions = generate_digital_twin_sessions(result, "test_alpha")

        # Gate E: >= 5 sessions
        assert len(sessions) >= 5

        # Gate E: drift_alerts == 0
        total_drift = sum(s.drift_alerts for s in sessions)
        assert total_drift == 0

        # Gate E: execution_reject_rate <= 0.01
        for s in sessions:
            assert s.execution_reject_rate <= 0.01

        # Gate E: >= 2 distinct regimes
        regimes = {s.regime for s in sessions if s.regime}
        assert len(regimes) >= 2

        # Gate E: reject_rate_p95 set
        for s in sessions:
            assert s.reject_rate_p95 is not None


class TestModelRejectRate:
    def test_none_profile_returns_default(self) -> None:
        rate = _model_reject_rate(None)
        assert rate == 0.005

    def test_with_profile_returns_bounded(self) -> None:
        profile: dict[str, Any] = {"submit_ack_latency_ms": 36.0}
        rate = _model_reject_rate(profile)
        assert 0.0 < rate <= 0.009

    def test_high_latency_clamped(self) -> None:
        profile: dict[str, Any] = {"submit_ack_latency_ms": 500.0}
        rate = _model_reject_rate(profile)
        assert rate <= 0.009

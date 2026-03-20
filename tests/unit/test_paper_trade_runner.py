"""Unit tests for alpha.paper_trade_runner — PaperTradeRunner and helpers."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from hft_platform.alpha.experiments import PaperTradeSession
from hft_platform.alpha.paper_trade_runner import (
    PaperTradeRunner,
    PaperTradeRunnerConfig,
    PaperTradeSummary,
    _build_summary,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_session(
    alpha_id: str = "test_alpha",
    session_id: str = "sess001",
    trading_day: str = "2026-03-01",
    pnl_bps: float = 5.0,
    fills: int = 10,
    drift_alerts: int = 0,
    execution_reject_rate: float = 0.01,
    duration_seconds: int = 14400,
    regime: str | None = "trending",
    reject_rate_p95: float | None = 0.015,
) -> PaperTradeSession:
    started_at = f"{trading_day}T09:00:00+00:00"
    ended_at = f"{trading_day}T13:00:00+00:00"
    return PaperTradeSession(
        alpha_id=alpha_id,
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        trading_day=trading_day,
        fills=fills,
        pnl_bps=pnl_bps,
        drift_alerts=drift_alerts,
        execution_reject_rate=execution_reject_rate,
        regime=regime,
        reject_rate_p95=reject_rate_p95,
    )


class _FixedSessionRunner:
    """Test double that returns pre-built sessions sequentially."""

    def __init__(self, sessions: list[PaperTradeSession]) -> None:
        self._sessions = list(sessions)
        self._idx = 0

    def run(
        self,
        alpha_id: str,
        duration_minutes: int,
        regime_hint: str | None = None,
    ) -> PaperTradeSession:
        session = self._sessions[self._idx % len(self._sessions)]
        self._idx += 1
        return session


def _cfg(**overrides: Any) -> PaperTradeRunnerConfig:
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "max_sessions": 3,
    }
    defaults.update(overrides)
    return PaperTradeRunnerConfig(**defaults)


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_empty_sessions(self) -> None:
        summary = _build_summary(alpha_id="a", sessions=[])
        assert summary.session_count == 0
        assert summary.calendar_span_days == 0
        assert summary.distinct_trading_days == 0

    def test_single_session(self) -> None:
        sessions = [_make_session(trading_day="2026-03-10")]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert summary.session_count == 1
        assert summary.calendar_span_days == 1
        assert summary.distinct_trading_days == 1

    def test_multiple_sessions_calendar_span(self) -> None:
        sessions = [
            _make_session(session_id="s1", trading_day="2026-03-10"),
            _make_session(session_id="s2", trading_day="2026-03-12"),
            _make_session(session_id="s3", trading_day="2026-03-14"),
        ]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert summary.session_count == 3
        assert summary.calendar_span_days == 5  # 10 to 14
        assert summary.distinct_trading_days == 3

    def test_drift_alerts_total(self) -> None:
        sessions = [
            _make_session(session_id="s1", drift_alerts=1),
            _make_session(session_id="s2", drift_alerts=2),
        ]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert summary.drift_alerts_total == 3  # 1 + 2

    def test_total_fills(self) -> None:
        sessions = [
            _make_session(session_id="s1", fills=5),
            _make_session(session_id="s2", fills=10),
        ]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert summary.total_fills == 15

    def test_mean_pnl_bps(self) -> None:
        sessions = [
            _make_session(session_id="s1", pnl_bps=4.0),
            _make_session(session_id="s2", pnl_bps=8.0),
        ]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert abs(summary.mean_pnl_bps - 6.0) < 1e-9

    def test_to_dict_keys(self) -> None:
        sessions = [_make_session()]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        d = summary.to_dict()
        assert "session_count" in d
        assert "execution_reject_rate_mean" in d
        assert "calendar_span_days" in d

    def test_duplicate_trading_days_counted_once(self) -> None:
        sessions = [
            _make_session(session_id="s1", trading_day="2026-03-01"),
            _make_session(session_id="s2", trading_day="2026-03-01"),
        ]
        summary = _build_summary(alpha_id="a", sessions=sessions)
        assert summary.distinct_trading_days == 1
        assert summary.calendar_span_days == 1

    def test_empty_sessions_returns_zero_stats(self) -> None:
        summary = _build_summary(alpha_id="a", sessions=[])
        assert summary.session_count == 0
        assert summary.drift_alerts_total == 0
        assert summary.execution_reject_rate_mean == 0.0
        assert summary.mean_pnl_bps == 0.0
        assert summary.min_session_duration_seconds == 0


# ---------------------------------------------------------------------------
# PaperTradeRunner.run_session
# ---------------------------------------------------------------------------


class TestRunSession:
    def test_returns_paper_trade_session(self) -> None:
        expected = _make_session(alpha_id="my_alpha", session_id="s1")
        runner = PaperTradeRunner(runner=_FixedSessionRunner([expected]))
        config = PaperTradeRunnerConfig(alpha_id="my_alpha", session_duration_minutes=60)
        result = runner.run_session(config)
        assert isinstance(result, PaperTradeSession)
        assert result.alpha_id == "my_alpha"

    def test_run_session_passes_duration_and_regime_hint(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run.return_value = _make_session(alpha_id="alpha_x")
        runner = PaperTradeRunner(runner=mock_runner)
        config = PaperTradeRunnerConfig(
            alpha_id="alpha_x",
            session_duration_minutes=120,
            regime_hint="volatile",
        )
        runner.run_session(config)
        mock_runner.run.assert_called_once_with(
            alpha_id="alpha_x",
            duration_minutes=120,
            regime_hint="volatile",
        )

    def test_run_session_no_tracker_still_works(self) -> None:
        runner = PaperTradeRunner(runner=_FixedSessionRunner([_make_session()]))
        config = PaperTradeRunnerConfig(alpha_id="alpha_y")
        result = runner.run_session(config)
        assert result.alpha_id == "test_alpha"


# ---------------------------------------------------------------------------
# PaperTradeRunner.run_campaign
# ---------------------------------------------------------------------------


class TestRunCampaign:
    def test_campaign_produces_correct_session_count(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(5)]
        runner = PaperTradeRunner(runner=_FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=5)
        summary = runner.run_campaign(config)
        assert summary.session_count == 5

    def test_campaign_respects_max_sessions(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(10)]
        runner = PaperTradeRunner(runner=_FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=3)
        summary = runner.run_campaign(config)
        assert summary.session_count == 3

    def test_no_tracker_returns_summary(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(4)]
        runner = PaperTradeRunner(runner=_FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="alpha_nt", max_sessions=4)
        summary = runner.run_campaign(config)
        assert isinstance(summary, PaperTradeSummary)
        assert summary.session_count == 4


# ---------------------------------------------------------------------------
# PaperTradeSummary.to_dict
# ---------------------------------------------------------------------------


class TestPaperTradeSummaryToDict:
    def test_to_dict_round_trips_json(self) -> None:
        sessions = [_make_session()]
        summary = _build_summary(alpha_id="alpha_rt", sessions=sessions)
        d = summary.to_dict()
        assert d["alpha_id"] == "alpha_rt"
        assert d["session_count"] == 1
        serialised = json.dumps(d)
        loaded = json.loads(serialised)
        assert loaded["alpha_id"] == "alpha_rt"

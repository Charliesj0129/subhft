"""Unit tests for alpha.paper_trade_runner — PaperTradeRunner and helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from hft_platform.alpha.experiments import ExperimentTracker, PaperTradeSession
from hft_platform.alpha.paper_trade_runner import (
    PaperTradeRunner,
    PaperTradeRunnerConfig,
    PaperTradeSummary,
    SessionRunner,
    _build_summary,
    _calendar_span,
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


class FixedSessionRunner:
    """A mock SessionRunner that returns a pre-configured session each call."""

    def __init__(self, sessions: list[PaperTradeSession]) -> None:
        self._sessions = list(sessions)
        self._index = 0

    def run(
        self,
        alpha_id: str,
        duration_minutes: int,
        regime_hint: str | None = None,
    ) -> PaperTradeSession:
        if not self._sessions:
            raise RuntimeError("No sessions configured")
        session = self._sessions[self._index % len(self._sessions)]
        self._index += 1
        return session


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSessionRunnerProtocol:
    def test_fixed_runner_satisfies_protocol(self) -> None:
        """FixedSessionRunner must satisfy the SessionRunner Protocol interface."""
        runner: SessionRunner = FixedSessionRunner([_make_session()])
        session = runner.run(alpha_id="a", duration_minutes=60)
        assert isinstance(session, PaperTradeSession)


# ---------------------------------------------------------------------------
# PaperTradeRunner.run_session
# ---------------------------------------------------------------------------


class TestRunSession:
    def test_returns_paper_trade_session(self) -> None:
        expected = _make_session(alpha_id="my_alpha", session_id="s1")
        runner = PaperTradeRunner(runner=FixedSessionRunner([expected]))
        config = PaperTradeRunnerConfig(alpha_id="my_alpha", session_duration_minutes=60)
        result = runner.run_session(config)
        assert isinstance(result, PaperTradeSession)
        assert result.alpha_id == "my_alpha"
        assert result.session_id == "s1"
        assert result.pnl_bps == expected.pnl_bps

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
        runner = PaperTradeRunner(runner=FixedSessionRunner([_make_session()]))
        config = PaperTradeRunnerConfig(alpha_id="alpha_y")
        result = runner.run_session(config)
        assert result.alpha_id == "test_alpha"


# ---------------------------------------------------------------------------
# PaperTradeRunner.run_campaign — session count
# ---------------------------------------------------------------------------


class TestRunCampaignSessionCount:
    def test_campaign_produces_correct_session_count(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i+1:02d}") for i in range(5)]
        runner = PaperTradeRunner(runner=FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=5)
        summary = runner.run_campaign(config)
        assert summary.session_count == 5
        assert len(summary.sessions) == 5

    def test_campaign_respects_max_sessions(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i+1:02d}") for i in range(10)]
        runner = PaperTradeRunner(runner=FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=3)
        summary = runner.run_campaign(config)
        assert summary.session_count == 3

    def test_campaign_zero_sessions_returns_empty_summary(self) -> None:
        runner = PaperTradeRunner(runner=FixedSessionRunner([_make_session()]))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=0)
        summary = runner.run_campaign(config)
        assert summary.session_count == 0
        assert summary.sessions == ()


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------


class TestSummaryStatistics:
    def _make_multi_day_sessions(self) -> list[PaperTradeSession]:
        return [
            _make_session(
                session_id="s1",
                trading_day="2026-03-01",
                pnl_bps=4.0,
                drift_alerts=1,
                execution_reject_rate=0.02,
                duration_seconds=7200,
            ),
            _make_session(
                session_id="s2",
                trading_day="2026-03-03",
                pnl_bps=6.0,
                drift_alerts=2,
                execution_reject_rate=0.04,
                duration_seconds=3600,
            ),
            _make_session(
                session_id="s3",
                trading_day="2026-03-05",
                pnl_bps=2.0,
                drift_alerts=0,
                execution_reject_rate=0.01,
                duration_seconds=14400,
            ),
        ]

    def test_calendar_span_days(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        # 2026-03-01 to 2026-03-05 = 5 calendar days (inclusive)
        assert summary.calendar_span_days == 5

    def test_distinct_trading_days(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        assert summary.distinct_trading_days == 3

    def test_drift_alerts_total(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        assert summary.drift_alerts_total == 3  # 1 + 2 + 0

    def test_execution_reject_rate_mean(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        expected_mean = (0.02 + 0.04 + 0.01) / 3
        assert abs(summary.execution_reject_rate_mean - expected_mean) < 1e-9

    def test_pnl_bps_mean(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        expected_mean = (4.0 + 6.0 + 2.0) / 3
        assert abs(summary.pnl_bps_mean - expected_mean) < 1e-9

    def test_min_session_duration_seconds(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        assert summary.min_session_duration_seconds == 3600  # min of 7200, 3600, 14400

    def test_session_count_matches(self) -> None:
        sessions = self._make_multi_day_sessions()
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        assert summary.session_count == 3

    def test_duplicate_trading_days_counted_once(self) -> None:
        sessions = [
            _make_session(session_id="s1", trading_day="2026-03-01"),
            _make_session(session_id="s2", trading_day="2026-03-01"),
        ]
        summary = _build_summary(alpha_id="alpha", sessions=sessions)
        assert summary.distinct_trading_days == 1
        # Same day twice → calendar span is 1
        assert summary.calendar_span_days == 1

    def test_empty_sessions_returns_zero_stats(self) -> None:
        summary = _build_summary(alpha_id="alpha", sessions=[])
        assert summary.session_count == 0
        assert summary.calendar_span_days == 0
        assert summary.distinct_trading_days == 0
        assert summary.drift_alerts_total == 0
        assert summary.execution_reject_rate_mean == 0.0
        assert summary.pnl_bps_mean == 0.0
        assert summary.min_session_duration_seconds == 0


# ---------------------------------------------------------------------------
# Persistence: campaign with tracker
# ---------------------------------------------------------------------------


class TestPersistenceWithTracker:
    def test_summary_file_created(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        sessions = [
            _make_session(session_id=f"s{i}", trading_day=f"2026-03-{i+1:02d}") for i in range(3)
        ]
        runner = PaperTradeRunner(
            runner=FixedSessionRunner(sessions),
            tracker=tracker,
        )
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=3)
        summary = runner.run_campaign(config)

        summary_path = tracker.paper_trade_dir / "test_alpha" / "paper_trade_summary.json"
        assert summary_path.exists(), f"Expected summary at {summary_path}"
        payload = json.loads(summary_path.read_text())
        assert payload["alpha_id"] == "test_alpha"
        assert payload["session_count"] == 3

    def test_session_files_created(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        sessions = [
            _make_session(session_id=f"s{i}", trading_day=f"2026-03-{i+1:02d}") for i in range(2)
        ]
        runner = PaperTradeRunner(
            runner=FixedSessionRunner(sessions),
            tracker=tracker,
        )
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=2)
        runner.run_campaign(config)

        session_dir = tracker.paper_trade_dir / "test_alpha" / "sessions"
        assert session_dir.exists()
        files = list(session_dir.glob("*.json"))
        assert len(files) == 2

    def test_tracker_summary_stats_match(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        sessions = [
            _make_session(
                session_id="s1",
                trading_day="2026-03-10",
                pnl_bps=8.0,
                drift_alerts=0,
                execution_reject_rate=0.005,
            ),
            _make_session(
                session_id="s2",
                trading_day="2026-03-12",
                pnl_bps=4.0,
                drift_alerts=0,
                execution_reject_rate=0.010,
            ),
        ]
        runner = PaperTradeRunner(
            runner=FixedSessionRunner(sessions),
            tracker=tracker,
        )
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=2)
        summary = runner.run_campaign(config)

        assert summary.session_count == 2
        assert abs(summary.pnl_bps_mean - 6.0) < 1e-9
        assert abs(summary.execution_reject_rate_mean - 0.0075) < 1e-9
        assert summary.drift_alerts_total == 0
        assert summary.calendar_span_days == 3  # 2026-03-10 to 2026-03-12


# ---------------------------------------------------------------------------
# Persistence: campaign without tracker
# ---------------------------------------------------------------------------


class TestCampaignWithoutTracker:
    def test_no_tracker_returns_summary(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i+1:02d}") for i in range(4)]
        runner = PaperTradeRunner(runner=FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="alpha_nt", max_sessions=4)
        summary = runner.run_campaign(config)
        assert isinstance(summary, PaperTradeSummary)
        assert summary.session_count == 4

    def test_no_tracker_no_files_created(self, tmp_path: Path) -> None:
        sessions = [_make_session()]
        runner = PaperTradeRunner(runner=FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="alpha_nt", max_sessions=1)
        runner.run_campaign(config)
        # No files should have been written anywhere under tmp_path
        all_files = list(tmp_path.rglob("*"))
        assert all_files == []


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
        # Verify JSON-serialisable
        serialised = json.dumps(d)
        loaded = json.loads(serialised)
        assert loaded["alpha_id"] == "alpha_rt"


# ---------------------------------------------------------------------------
# _calendar_span helper
# ---------------------------------------------------------------------------


class TestCalendarSpan:
    def test_single_day(self) -> None:
        assert _calendar_span(["2026-03-01"]) == 1

    def test_empty(self) -> None:
        assert _calendar_span([]) == 0

    def test_two_consecutive_days(self) -> None:
        assert _calendar_span(["2026-03-01", "2026-03-02"]) == 2

    def test_five_day_span(self) -> None:
        assert _calendar_span(["2026-03-01", "2026-03-05"]) == 5

    def test_invalid_date_returns_zero(self) -> None:
        assert _calendar_span(["not-a-date", "2026-03-05"]) == 0

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


def _cfg(**overrides: Any) -> PaperTradeRunnerConfig:
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "target_sessions": 3,
        "min_session_minutes": 60,
    }
    defaults.update(overrides)
    return PaperTradeRunnerConfig(**defaults)


# ---------------------------------------------------------------------------
# _compute_summary
# ---------------------------------------------------------------------------


class TestComputeSummary:
    def test_empty_sessions(self) -> None:
        cfg = _cfg()
        summary = _compute_summary("a", "c1", [], cfg)
        assert summary.session_count == 0
        assert summary.calendar_span_days == 0
        assert summary.distinct_trading_days == 0

    def test_single_session(self) -> None:
        sessions = [_make_session(trading_day="2026-03-10")]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.session_count == 1
        assert summary.calendar_span_days == 1
        assert summary.distinct_trading_days == 1

    def test_multiple_sessions_calendar_span(self) -> None:
        sessions = [
            _make_session(session_id="s1", trading_day="2026-03-10"),
            _make_session(session_id="s2", trading_day="2026-03-12"),
            _make_session(session_id="s3", trading_day="2026-03-14"),
        ]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.session_count == 3
        assert summary.calendar_span_days == 5  # 10 to 14
        assert summary.distinct_trading_days == 3

    def test_invalid_sessions_count(self) -> None:
        cfg = _cfg(min_session_minutes=60)
        sessions = [
            _make_session(session_id="s1", duration_seconds=7200),  # valid
            _make_session(session_id="s2", duration_seconds=300),  # invalid (5 min)
        ]
        summary = _compute_summary("a", "c1", sessions, cfg)
        assert summary.invalid_session_duration_count == 1

    def test_drift_alerts_total(self) -> None:
        sessions = [
            _make_session(session_id="s1", drift_alerts=1),
            _make_session(session_id="s2", drift_alerts=2),
        ]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.drift_alerts_total == 3

    def test_reject_rate_mean(self) -> None:
        sessions = [
            _make_session(session_id="s1", reject_rate=0.01),
            _make_session(session_id="s2", reject_rate=0.03),
        ]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.execution_reject_rate_mean == pytest.approx(0.02)

    def test_reject_rate_p95_none_when_not_recorded(self) -> None:
        sessions = [_make_session(session_id="s1")]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.execution_reject_rate_p95 is None

    def test_total_fills(self) -> None:
        sessions = [
            _make_session(session_id="s1", fills=5),
            _make_session(session_id="s2", fills=10),
        ]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.total_fills == 15

    def test_mean_pnl_bps(self) -> None:
        sessions = [
            _make_session(session_id="s1", pnl_bps=4.0),
            _make_session(session_id="s2", pnl_bps=8.0),
        ]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        assert summary.mean_pnl_bps == pytest.approx(6.0)

    def test_to_dict_keys(self) -> None:
        sessions = [_make_session()]
        summary = _compute_summary("a", "c1", sessions, _cfg())
        d = summary.to_dict()
        assert "session_count" in d
        assert "execution_reject_rate_mean" in d
        assert "calendar_span_days" in d


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
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(5)]
        runner = PaperTradeRunner(runner=FixedSessionRunner(sessions))
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=5)
        summary = runner.run_campaign(config)
        assert summary.session_count == 5
        assert len(summary.sessions) == 5

    def test_campaign_respects_max_sessions(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(10)]
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
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(3)]
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
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(2)]
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
        sessions = [_make_session(session_id=f"s{i}", trading_day=f"2026-03-{i + 1:02d}") for i in range(4)]
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

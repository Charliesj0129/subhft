"""Tests for PaperTradeRunner."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.alpha.experiments import PaperTradeSession
from hft_platform.alpha.paper_trade_runner import (
    PaperTradeRunner,
    PaperTradeRunnerConfig,
    SessionRunner,
    _compute_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    alpha_id: str = "test_alpha",
    session_id: str = "s1",
    trading_day: str = "2026-03-10",
    duration_seconds: int = 3600,
    fills: int = 10,
    pnl_bps: float = 5.0,
    drift_alerts: int = 0,
    reject_rate: float = 0.001,
    reject_rate_p95: float | None = None,
) -> PaperTradeSession:
    now = dt.datetime.now(dt.UTC).isoformat()
    return PaperTradeSession(
        alpha_id=alpha_id,
        session_id=session_id,
        started_at=now,
        ended_at=now,
        duration_seconds=duration_seconds,
        trading_day=trading_day,
        fills=fills,
        pnl_bps=pnl_bps,
        drift_alerts=drift_alerts,
        execution_reject_rate=reject_rate,
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
    def test_dry_run_returns_stub(self) -> None:
        mock_runner = MagicMock(spec=SessionRunner)
        runner = PaperTradeRunner(mock_runner)
        cfg = _cfg(dry_run=True)
        session = runner.run_session(cfg)
        assert session.notes == "dry_run"
        assert session.fills == 0
        mock_runner.run_one_session.assert_not_called()

    def test_successful_session(self) -> None:
        expected = _make_session()
        mock_runner = MagicMock(spec=SessionRunner)
        mock_runner.run_one_session.return_value = expected
        runner = PaperTradeRunner(mock_runner)
        result = runner.run_session(_cfg())
        assert result is expected

    def test_retries_on_error(self) -> None:
        expected = _make_session()
        mock_runner = MagicMock(spec=SessionRunner)
        mock_runner.run_one_session.side_effect = [ValueError("oops"), expected]
        runner = PaperTradeRunner(mock_runner)
        result = runner.run_session(_cfg(max_retries=2))
        assert result is expected
        assert mock_runner.run_one_session.call_count == 2

    def test_raises_after_max_retries(self) -> None:
        mock_runner = MagicMock(spec=SessionRunner)
        mock_runner.run_one_session.side_effect = ValueError("always fails")
        runner = PaperTradeRunner(mock_runner)
        with pytest.raises(RuntimeError, match="run_session failed"):
            runner.run_session(_cfg(max_retries=2))
        assert mock_runner.run_one_session.call_count == 2


# ---------------------------------------------------------------------------
# PaperTradeRunner.run_campaign
# ---------------------------------------------------------------------------


class TestRunCampaign:
    def test_runs_target_sessions(self) -> None:
        sessions = [_make_session(session_id=f"s{i}", trading_day="2026-03-10") for i in range(5)]
        mock_runner = MagicMock(spec=SessionRunner)
        mock_runner.run_one_session.side_effect = sessions
        runner = PaperTradeRunner(mock_runner)
        summary = runner.run_campaign(_cfg(target_sessions=5))
        assert summary.session_count == 5
        assert mock_runner.run_one_session.call_count == 5

    def test_campaign_summary_stats(self) -> None:
        sessions = [
            _make_session(session_id=f"s{i}", trading_day=f"2026-03-1{i}", fills=10, pnl_bps=5.0) for i in range(3)
        ]
        mock_runner = MagicMock(spec=SessionRunner)
        mock_runner.run_one_session.side_effect = sessions
        runner = PaperTradeRunner(mock_runner)
        summary = runner.run_campaign(_cfg(target_sessions=3))
        assert summary.total_fills == 30
        assert summary.mean_pnl_bps == pytest.approx(5.0)

    def test_dry_run_campaign(self) -> None:
        mock_runner = MagicMock(spec=SessionRunner)
        runner = PaperTradeRunner(mock_runner)
        summary = runner.run_campaign(_cfg(dry_run=True, target_sessions=3))
        assert summary.session_count == 3
        mock_runner.run_one_session.assert_not_called()

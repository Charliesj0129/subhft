"""Unit tests for Gate E batch campaign wiring.

Tests cover:
- Adapter constructing valid PaperTradeRunnerConfig from alpha_id
- campaign_runner callable is properly wired via make_paper_trade_campaign_runner
- Candidate discovery logic (Gate-D-passed, Gate-E-pending)
- PaperTradeRunner is mocked to avoid actual trading
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock  # noqa: F401 — kept for future mock-based tests

import pytest

from hft_platform.alpha.experiments import PaperTradeSession
from hft_platform.alpha.gate_e_batch import (
    GateEBatchConfig,
    GateEBatchReport,
    GateEBatchRunner,
    _StubSessionRunner,
    discover_gate_e_candidates,
    make_paper_trade_campaign_runner,
)
from hft_platform.alpha.paper_trade_runner import (
    PaperTradeRunner,
    PaperTradeRunnerConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROMOTIONS_SUBPATH = ("research", "experiments", "promotions")


def _make_decision(
    tmp_path: Path,
    alpha_id: str,
    gate_d_passed: bool,
    gate_e_passed: bool,
    timestamp: str = "20260101T000000Z_aabbccdd",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a promotion_decision.json under the expected directory structure."""
    decision_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, alpha_id, timestamp)
    decision_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "alpha_id": alpha_id,
        "gate_d_passed": gate_d_passed,
        "gate_e_passed": gate_e_passed,
        "approved": gate_d_passed and gate_e_passed,
        "reasons": [],
    }
    if extra:
        payload.update(extra)
    decision_path = decision_dir / "promotion_decision.json"
    decision_path.write_text(json.dumps(payload), encoding="utf-8")
    return decision_path


def _make_stub_session(alpha_id: str = "test_alpha", session_id: str = "sess001") -> PaperTradeSession:
    """Create a minimal PaperTradeSession for testing."""
    return PaperTradeSession(
        alpha_id=alpha_id,
        session_id=session_id,
        started_at="2026-01-01T09:00:00+00:00",
        ended_at="2026-01-01T13:00:00+00:00",
        duration_seconds=14400,
        trading_day="2026-01-01",
        fills=10,
        pnl_bps=5.0,
        drift_alerts=0,
        execution_reject_rate=0.0,
        notes="test session",
    )


# ---------------------------------------------------------------------------
# PaperTradeRunnerConfig adapter construction
# ---------------------------------------------------------------------------


class TestPaperTradeRunnerConfigConstruction:
    """Verify that make_paper_trade_campaign_runner builds a valid config."""

    def test_campaign_runner_callable_returns_callable(self, tmp_path: Path) -> None:
        """make_paper_trade_campaign_runner returns a callable."""
        runner = make_paper_trade_campaign_runner(project_root=tmp_path)
        assert callable(runner)

    def test_campaign_runner_calls_paper_trade_runner(self, tmp_path: Path) -> None:
        """campaign_runner invokes PaperTradeRunner.run_campaign with correct alpha_id.

        We use _StubSessionRunner to avoid any real paper-trade execution.
        The campaign runner is called end-to-end and we verify the summary
        reflects the correct alpha_id.
        """
        campaign_runner = make_paper_trade_campaign_runner(
            project_root=tmp_path,
            max_sessions=1,
            session_duration_minutes=120,
        )
        # run_campaign dispatches through stub runner — no actual trading
        summary = campaign_runner("my_alpha")

        assert summary is not None
        assert summary.alpha_id == "my_alpha"

    def test_campaign_runner_config_alpha_id_matches(self, tmp_path: Path) -> None:
        """The config built by the adapter uses the exact alpha_id passed."""
        campaign_runner = make_paper_trade_campaign_runner(
            project_root=tmp_path,
            max_sessions=1,
        )
        summary = campaign_runner("alpha_xyz_42")
        assert summary.alpha_id == "alpha_xyz_42"

    def test_campaign_runner_uses_default_session_duration(self, tmp_path: Path) -> None:
        """Default session_duration_minutes is 240 — reflected in session duration."""
        campaign_runner = make_paper_trade_campaign_runner(
            project_root=tmp_path,
            max_sessions=1,
            # don't override session_duration_minutes — use default (240)
        )
        summary = campaign_runner("some_alpha")
        # Stub runner creates sessions with duration = duration_minutes * 60
        assert summary.min_session_duration_seconds == 240 * 60

    def test_campaign_runner_uses_custom_max_sessions(self, tmp_path: Path) -> None:
        """max_sessions controls how many sessions are run per campaign."""
        campaign_runner = make_paper_trade_campaign_runner(
            project_root=tmp_path,
            max_sessions=3,
            session_duration_minutes=60,
        )
        summary = campaign_runner("some_alpha")
        assert summary.session_count == 3

    def test_campaign_runner_raises_on_empty_alpha_id(self, tmp_path: Path) -> None:
        """Passing an empty alpha_id raises ValueError."""
        runner = make_paper_trade_campaign_runner(project_root=tmp_path)
        with pytest.raises(ValueError, match="alpha_id"):
            runner("")

    def test_campaign_runner_raises_on_whitespace_alpha_id(self, tmp_path: Path) -> None:
        """Passing a whitespace-only alpha_id raises ValueError."""
        runner = make_paper_trade_campaign_runner(project_root=tmp_path)
        with pytest.raises(ValueError, match="alpha_id"):
            runner("   ")


# ---------------------------------------------------------------------------
# Campaign runner wiring in GateEBatchRunner
# ---------------------------------------------------------------------------


class TestCampaignRunnerWiring:
    """Verify that GateEBatchRunner correctly dispatches to campaign_runner."""

    def test_wired_runner_called_for_each_candidate(self, tmp_path: Path) -> None:
        """campaign_runner is called once per qualifying candidate."""
        _make_decision(tmp_path, "alpha_a", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_b", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_done", gate_d_passed=True, gate_e_passed=True)

        called: list[str] = []

        def campaign_runner(alpha_id: str) -> None:
            called.append(alpha_id)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        GateEBatchRunner(campaign_runner=campaign_runner).run(config)

        assert set(called) == {"alpha_a", "alpha_b"}

    def test_paper_trade_runner_wired_as_campaign_runner(self, tmp_path: Path) -> None:
        """make_paper_trade_campaign_runner produces a valid campaign runner for GateEBatchRunner.

        Uses the stub session runner (no real trading) to verify end-to-end wiring.
        """
        _make_decision(tmp_path, "wired_alpha", gate_d_passed=True, gate_e_passed=False)

        # Build the wired campaign runner using the factory (uses _StubSessionRunner internally)
        campaign_runner = make_paper_trade_campaign_runner(
            project_root=tmp_path,
            max_sessions=1,
            session_duration_minutes=60,
        )
        batch_runner = GateEBatchRunner(campaign_runner=campaign_runner)
        report = batch_runner.run(GateEBatchConfig(project_root=tmp_path, dry_run=False))

        assert "wired_alpha" in report.completed
        assert report.failed == ()
        assert report.skipped == ()

    def test_failing_campaign_runner_marks_failed(self, tmp_path: Path) -> None:
        """If campaign_runner raises, the alpha appears in 'failed'."""
        _make_decision(tmp_path, "bad_alpha", gate_d_passed=True, gate_e_passed=False)

        def failing_runner(alpha_id: str) -> None:
            raise RuntimeError("paper trade failed")

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner(campaign_runner=failing_runner).run(config)

        assert "bad_alpha" in report.failed
        assert report.completed == ()

    def test_none_runner_skips_all_candidates(self, tmp_path: Path) -> None:
        """When campaign_runner is None, all candidates are recorded as skipped."""
        _make_decision(tmp_path, "alpha_1", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_2", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=False)
        report = GateEBatchRunner(campaign_runner=None).run(config)

        assert set(report.skipped) == {"alpha_1", "alpha_2"}
        assert report.completed == ()
        assert report.failed == ()

    def test_dry_run_does_not_invoke_campaign_runner(self, tmp_path: Path) -> None:
        """In dry-run mode, campaign_runner is never called."""
        _make_decision(tmp_path, "alpha_dry", gate_d_passed=True, gate_e_passed=False)

        calls: list[str] = []

        def tracking_runner(alpha_id: str) -> None:
            calls.append(alpha_id)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        GateEBatchRunner(campaign_runner=tracking_runner).run(config)

        assert calls == []


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


class TestCandidateDiscovery:
    def test_discovers_gate_d_passed_gate_e_not_passed(self, tmp_path: Path) -> None:
        """Only Gate-D-passed, Gate-E-not-passed alphas are returned."""
        _make_decision(tmp_path, "alpha_ok", gate_d_passed=True, gate_e_passed=False)
        _make_decision(tmp_path, "alpha_both", gate_d_passed=True, gate_e_passed=True)
        _make_decision(tmp_path, "alpha_d_fail", gate_d_passed=False, gate_e_passed=False)

        candidates = discover_gate_e_candidates(tmp_path)

        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "alpha_ok"

    def test_missing_promotions_dir_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty list when promotions directory does not exist."""
        candidates = discover_gate_e_candidates(tmp_path)
        assert candidates == []

    def test_invalid_json_skipped_gracefully(self, tmp_path: Path) -> None:
        """Malformed JSON files are skipped; valid candidates still returned."""
        bad_dir = tmp_path.joinpath(*_PROMOTIONS_SUBPATH, "bad_alpha", "20260101T000000Z_00000000")
        bad_dir.mkdir(parents=True)
        (bad_dir / "promotion_decision.json").write_text("NOT JSON {{{{", encoding="utf-8")

        _make_decision(tmp_path, "good_alpha", gate_d_passed=True, gate_e_passed=False)

        candidates = discover_gate_e_candidates(tmp_path)
        assert len(candidates) == 1
        assert candidates[0]["alpha_id"] == "good_alpha"

    def test_multiple_runs_per_alpha_all_included(self, tmp_path: Path) -> None:
        """Multiple timestamp dirs for same alpha are treated independently."""
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260101T000000Z_aaa"
        )
        _make_decision(
            tmp_path, "repeat_alpha", gate_d_passed=True, gate_e_passed=False, timestamp="20260102T000000Z_bbb"
        )

        candidates = discover_gate_e_candidates(tmp_path)
        assert len(candidates) == 2


# ---------------------------------------------------------------------------
# Stub session runner
# ---------------------------------------------------------------------------


class TestStubSessionRunner:
    def test_stub_runner_returns_paper_trade_session(self) -> None:
        """StubSessionRunner.run returns a valid PaperTradeSession."""
        stub = _StubSessionRunner()
        session = stub.run(alpha_id="test_alpha", duration_minutes=60)

        assert isinstance(session, PaperTradeSession)
        assert session.alpha_id == "test_alpha"
        assert session.duration_seconds == 3600

    def test_stub_runner_propagates_regime_hint(self) -> None:
        """regime_hint is forwarded as the session's regime field."""
        stub = _StubSessionRunner()
        session = stub.run(alpha_id="test_alpha", duration_minutes=120, regime_hint="trending")

        assert session.regime == "trending"

    def test_stub_runner_session_id_is_non_empty(self) -> None:
        """Each session produced by the stub has a non-empty session_id."""
        stub = _StubSessionRunner()
        session = stub.run(alpha_id="test_alpha", duration_minutes=60)

        assert session.session_id
        assert len(session.session_id) > 0


# ---------------------------------------------------------------------------
# PaperTradeRunner unit tests
# ---------------------------------------------------------------------------


class TestPaperTradeRunner:
    def test_run_campaign_executes_max_sessions(self) -> None:
        """run_campaign calls the underlying runner exactly max_sessions times."""
        call_count = 0

        class CountingRunner:
            def run(
                self,
                alpha_id: str,
                duration_minutes: int,
                regime_hint: str | None = None,
            ) -> PaperTradeSession:
                nonlocal call_count
                call_count += 1
                return _make_stub_session(alpha_id=alpha_id, session_id=f"s{call_count}")

        runner = PaperTradeRunner(runner=CountingRunner())
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=3)
        summary = runner.run_campaign(config)

        assert call_count == 3
        assert summary.session_count == 3

    def test_run_campaign_aggregates_sessions(self) -> None:
        """Summary statistics aggregate data from all completed sessions."""

        class FixedRunner:
            def run(
                self,
                alpha_id: str,
                duration_minutes: int,
                regime_hint: str | None = None,
            ) -> PaperTradeSession:
                return PaperTradeSession(
                    alpha_id=alpha_id,
                    session_id="s1",
                    started_at="2026-01-01T09:00:00+00:00",
                    ended_at="2026-01-01T13:00:00+00:00",
                    duration_seconds=14400,
                    trading_day="2026-01-01",
                    fills=5,
                    pnl_bps=2.0,
                    drift_alerts=1,
                    execution_reject_rate=0.05,
                )

        runner = PaperTradeRunner(runner=FixedRunner())
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=2)
        summary = runner.run_campaign(config)

        assert summary.session_count == 2
        assert summary.drift_alerts_total == 2
        assert abs(summary.execution_reject_rate_mean - 0.05) < 1e-9

    def test_run_session_returns_single_session(self) -> None:
        """run_session returns a PaperTradeSession without running a full campaign."""
        stub = _StubSessionRunner()
        runner = PaperTradeRunner(runner=stub)
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", session_duration_minutes=60)

        session = runner.run_session(config)

        assert isinstance(session, PaperTradeSession)
        assert session.alpha_id == "test_alpha"

    def test_run_campaign_handles_session_errors(self) -> None:
        """Errors in individual sessions are caught; remaining sessions continue."""
        call_count = 0

        class FlakyRunner:
            def run(
                self,
                alpha_id: str,
                duration_minutes: int,
                regime_hint: str | None = None,
            ) -> PaperTradeSession:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("session 2 failed")
                return _make_stub_session(alpha_id=alpha_id, session_id=f"s{call_count}")

        runner = PaperTradeRunner(runner=FlakyRunner())
        config = PaperTradeRunnerConfig(alpha_id="test_alpha", max_sessions=3)
        summary = runner.run_campaign(config)

        # Only 2 sessions succeed (session 2 errored out)
        assert summary.session_count == 2

    def test_run_campaign_persists_summary_with_tracker(self, tmp_path: Path) -> None:
        """When a tracker is provided, the campaign summary is persisted to disk."""
        from hft_platform.alpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        stub = _StubSessionRunner()
        runner = PaperTradeRunner(runner=stub, tracker=tracker)
        config = PaperTradeRunnerConfig(alpha_id="persist_test", max_sessions=1)
        runner.run_campaign(config)

        summary_path = tracker.paper_trade_dir / "persist_test" / "paper_trade_summary.json"
        assert summary_path.exists()
        payload = json.loads(summary_path.read_text())
        assert payload["alpha_id"] == "persist_test"
        assert payload["session_count"] == 1


# ---------------------------------------------------------------------------
# GateEBatchReport structure
# ---------------------------------------------------------------------------


class TestGateEBatchReport:
    def test_report_is_frozen_dataclass(self) -> None:
        """GateEBatchReport is immutable (frozen=True)."""
        report = GateEBatchReport(candidates=(), completed=(), failed=(), skipped=())
        with pytest.raises(AttributeError):
            report.completed = ("oops",)  # type: ignore[misc]

    def test_report_writes_json_file(self, tmp_path: Path) -> None:
        """GateEBatchRunner writes gate_e_batch_report.json with expected keys."""
        _make_decision(tmp_path, "alpha_j", gate_d_passed=True, gate_e_passed=False)

        config = GateEBatchConfig(project_root=tmp_path, dry_run=True)
        GateEBatchRunner().run(config)

        report_path = tmp_path / "research" / "experiments" / "gate_e_batch_report.json"
        assert report_path.exists()
        payload = json.loads(report_path.read_text())
        assert set(payload.keys()) >= {"candidates", "completed", "failed", "skipped"}

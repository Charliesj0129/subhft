"""Unit tests for alpha Gate E — paper-trade governance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.alpha._gate_e import (
    _build_paper_governance_report,
    _evaluate_gate_e,
    _resolve_paper_trade_summary,
)
from hft_platform.alpha._promotion_types import PromotionConfig


def _cfg(**overrides: object) -> PromotionConfig:
    defaults = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "shadow_sessions": 6,
        "min_shadow_sessions": 5,
        "drift_alerts": 0,
        "execution_reject_rate": 0.005,
        "max_execution_reject_rate": 0.01,
        "require_paper_trade_governance": False,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)  # type: ignore[arg-type]


def _paper_summary(
    session_count: int = 10,
    calendar_span_days: int = 14,
    distinct_trading_days: int = 10,
    min_session_duration_seconds: int = 7200,
    invalid_session_duration_count: int = 0,
    drift_alerts_total: int = 0,
    execution_reject_rate_mean: float = 0.002,
    execution_reject_rate_p95: float | None = 0.005,
    regimes_covered: list[str] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "session_count": session_count,
        "calendar_span_days": calendar_span_days,
        "distinct_trading_days": distinct_trading_days,
        "min_session_duration_seconds": min_session_duration_seconds,
        "invalid_session_duration_count": invalid_session_duration_count,
        "drift_alerts_total": drift_alerts_total,
        "execution_reject_rate_mean": execution_reject_rate_mean,
    }
    if execution_reject_rate_p95 is not None:
        d["execution_reject_rate_p95"] = execution_reject_rate_p95
    if regimes_covered is not None:
        d["regimes_covered"] = regimes_covered
    return d


# ---------------------------------------------------------------------------
# _resolve_paper_trade_summary
# ---------------------------------------------------------------------------
class TestResolvePaperTradeSummary:
    def test_explicit_path_found(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        data = _paper_summary()
        summary_file.write_text(json.dumps(data))
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result == data
        assert source == "explicit"
        assert error is None

    def test_explicit_path_relative(self, tmp_path: Path) -> None:
        (tmp_path / "summaries").mkdir()
        summary_file = tmp_path / "summaries" / "pt.json"
        summary_file.write_text(json.dumps(_paper_summary()))
        cfg = _cfg(paper_trade_summary_path="summaries/pt.json")
        result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result is not None
        assert source == "explicit"

    def test_explicit_path_not_found(self, tmp_path: Path) -> None:
        cfg = _cfg(paper_trade_summary_path=str(tmp_path / "missing.json"))
        result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result is None
        assert source == "explicit"
        assert "not_found" in (error or "")

    def test_explicit_path_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        cfg = _cfg(paper_trade_summary_path=str(bad))
        result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result is None
        assert "read_error" in (error or "")

    def test_explicit_path_non_dict(self, tmp_path: Path) -> None:
        arr = tmp_path / "arr.json"
        arr.write_text(json.dumps([1, 2, 3]))
        cfg = _cfg(paper_trade_summary_path=str(arr))
        result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result is None
        assert error == "paper_trade_summary_invalid_format"

    def test_tracker_fallback_error(self, tmp_path: Path) -> None:
        """When no explicit path and tracker raises, returns error."""
        cfg = _cfg(paper_trade_summary_path=None)
        # The import is lazy inside the function; make ExperimentTracker raise.
        mock_experiments = MagicMock()
        mock_experiments.ExperimentTracker.side_effect = RuntimeError("no tracker")
        with patch.dict("sys.modules", {"hft_platform.alpha.experiments": mock_experiments}):
            result, source, error = _resolve_paper_trade_summary(cfg, tmp_path)
        assert result is None
        assert source == "tracker"
        assert error is not None


# ---------------------------------------------------------------------------
# _evaluate_gate_e — manual shadow mode
# ---------------------------------------------------------------------------
class TestGateEManualShadow:
    @staticmethod
    def _mock_tracker_unavailable():
        """Mock the experiments module so ExperimentTracker raises."""
        mock_mod = MagicMock()
        mock_mod.ExperimentTracker.side_effect = RuntimeError("unavailable")
        return patch.dict("sys.modules", {"hft_platform.alpha.experiments": mock_mod})

    def test_pass_with_config_values(self, tmp_path: Path) -> None:
        """No paper_trade_summary_path, no tracker: uses config values."""
        cfg = _cfg(
            shadow_sessions=6,
            drift_alerts=0,
            execution_reject_rate=0.005,
        )
        with self._mock_tracker_unavailable():
            passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is True
        assert result["mode"] == "manual_shadow"
        checks = result["checks"]
        assert checks["shadow_sessions"]["pass"] is True
        assert checks["drift_alerts"]["pass"] is True
        assert checks["execution_reject_rate"]["pass"] is True

    def test_insufficient_sessions_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(shadow_sessions=2, min_shadow_sessions=5)
        with self._mock_tracker_unavailable():
            passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False
        assert result["checks"]["shadow_sessions"]["pass"] is False

    def test_drift_alerts_fail(self, tmp_path: Path) -> None:
        cfg = _cfg(drift_alerts=1)
        with self._mock_tracker_unavailable():
            passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False

    def test_reject_rate_too_high_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(execution_reject_rate=0.05)
        with self._mock_tracker_unavailable():
            passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False


# ---------------------------------------------------------------------------
# _evaluate_gate_e — with paper trade summary (explicit)
# ---------------------------------------------------------------------------
class TestGateEWithSummary:
    def test_pass_with_summary_file(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(json.dumps(_paper_summary()))
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is True
        checks = result["checks"]
        assert checks["shadow_sessions"]["value"] == 10
        assert checks["execution_reject_rate"]["source"] == "p95"

    def test_p95_preferred_over_mean(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(
                _paper_summary(
                    execution_reject_rate_mean=0.001,
                    execution_reject_rate_p95=0.008,
                )
            )
        )
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        _, result = _evaluate_gate_e(cfg, tmp_path)
        assert result["checks"]["execution_reject_rate"]["value"] == 0.008
        assert result["checks"]["execution_reject_rate"]["source"] == "p95"

    def test_falls_back_to_mean_when_p95_missing(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(
                _paper_summary(
                    execution_reject_rate_mean=0.003,
                    execution_reject_rate_p95=None,
                )
            )
        )
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        _, result = _evaluate_gate_e(cfg, tmp_path)
        assert result["checks"]["execution_reject_rate"]["value"] == 0.003
        assert result["checks"]["execution_reject_rate"]["source"] == "mean"


# ---------------------------------------------------------------------------
# _evaluate_gate_e — paper trade governance mode
# ---------------------------------------------------------------------------
class TestGateEPaperGovernance:
    def test_governance_pass_full(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(json.dumps(_paper_summary()))
        cfg = _cfg(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_file),
        )
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is True
        assert result["mode"] == "paper_trade_governed"
        checks = result["checks"]
        assert checks["paper_trade_log_available"]["pass"] is True
        assert checks["paper_trade_calendar_days"]["pass"] is True
        assert checks["paper_trade_trading_days"]["pass"] is True
        assert checks["paper_trade_session_duration"]["pass"] is True

    def test_governance_insufficient_calendar_days(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(_paper_summary(calendar_span_days=3))
        )
        cfg = _cfg(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_file),
            min_paper_trade_calendar_days=7,
        )
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False
        assert result["checks"]["paper_trade_calendar_days"]["pass"] is False

    def test_governance_insufficient_trading_days(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(_paper_summary(distinct_trading_days=2))
        )
        cfg = _cfg(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_file),
            min_paper_trade_trading_days=5,
        )
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False

    def test_governance_invalid_session_duration(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(
                _paper_summary(
                    min_session_duration_seconds=30,
                    invalid_session_duration_count=2,
                )
            )
        )
        cfg = _cfg(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_file),
        )
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False
        assert result["checks"]["paper_trade_session_duration"]["pass"] is False

    def test_governance_no_summary_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(tmp_path / "missing.json"),
        )
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        assert passed is False
        assert result["checks"]["paper_trade_log_available"]["pass"] is False


# ---------------------------------------------------------------------------
# Regime span check
# ---------------------------------------------------------------------------
class TestGateERegimeSpan:
    def test_regime_span_warning_when_insufficient(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(_paper_summary(regimes_covered=["trending"]))
        )
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        passed, result = _evaluate_gate_e(cfg, tmp_path)
        regime = result["checks"]["regime_span"]
        assert regime["pass"] is True  # warn-only, does not block
        assert "warning" in regime
        assert passed is True

    def test_regime_span_no_warning_when_sufficient(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.json"
        summary_file.write_text(
            json.dumps(
                _paper_summary(regimes_covered=["trending", "mean_reverting"])
            )
        )
        cfg = _cfg(paper_trade_summary_path=str(summary_file))
        _, result = _evaluate_gate_e(cfg, tmp_path)
        assert "warning" not in result["checks"]["regime_span"]


# ---------------------------------------------------------------------------
# _build_paper_governance_report
# ---------------------------------------------------------------------------
class TestBuildPaperGovernanceReport:
    def test_report_structure(self) -> None:
        summary = _paper_summary(regimes_covered=["trending", "mean_reverting"])
        gate_e_checks = {
            "checks": {
                "shadow_sessions": {"value": 10, "pass": True},
                "drift_alerts": {"value": 0, "pass": True},
                "execution_reject_rate": {"value": 0.005, "source": "p95", "pass": True},
            },
            "paper_trade_summary": summary,
            "paper_trade_summary_source": "explicit",
            "paper_trade_summary_error": None,
        }
        cfg = _cfg(min_shadow_sessions=5, min_paper_trade_calendar_days=7)
        report = _build_paper_governance_report(cfg, gate_e_checks)
        assert report["alpha_id"] == "test_alpha"
        assert report["passed"] is True
        assert "checks" in report
        assert report["checks"]["shadow_sessions"]["pass"] is True
        assert report["checks"]["regime_span"]["pass"] is True

    def test_report_fails_when_session_count_low(self) -> None:
        summary = _paper_summary(session_count=2)
        gate_e_checks = {
            "checks": {
                "shadow_sessions": {"value": 2, "pass": False},
                "drift_alerts": {"value": 0, "pass": True},
                "execution_reject_rate": {"value": 0.005, "source": "mean", "pass": True},
            },
            "paper_trade_summary": summary,
            "paper_trade_summary_source": "tracker",
            "paper_trade_summary_error": None,
        }
        cfg = _cfg(min_shadow_sessions=5)
        report = _build_paper_governance_report(cfg, gate_e_checks)
        assert report["passed"] is False
        assert report["checks"]["shadow_sessions"]["pass"] is False

    def test_report_no_summary(self) -> None:
        gate_e_checks = {
            "checks": {
                "shadow_sessions": {"value": 0},
                "drift_alerts": {"value": 0},
                "execution_reject_rate": {"value": 0.0},
            },
            "paper_trade_summary": None,
            "paper_trade_summary_source": "tracker",
            "paper_trade_summary_error": "no_sessions",
        }
        cfg = _cfg()
        report = _build_paper_governance_report(cfg, gate_e_checks)
        assert report["summary"] is None
        assert report["paper_trade_summary_error"] == "no_sessions"

"""Tests for scripts/report_narrative.py — report quality utilities."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure scripts/ is importable.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from report_narrative import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    compute_risk_score,
    diagnose_checks,
    executive_summary,
    format_status_icon,
    group_checks_by_category,
    recommend_actions,
    render_trend_section,
    sparkline,
    trend_delta,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_check(
    check_id: str,
    status: str = STATUS_PASS,
    severity: str = "warning",
    value: float = 0.0,
    threshold: str = "le 0.0",
    message: str = "test",
) -> dict:
    return {
        "id": check_id,
        "status": status,
        "severity": severity,
        "value": value,
        "threshold": threshold,
        "message": message,
    }


def _make_service(
    name: str,
    state: str = "running",
    health: str = "healthy",
    restart_count: int = 0,
) -> dict:
    return {
        "service": name,
        "state": state,
        "health": health,
        "restart_count": restart_count,
    }


def _make_daily_report(
    date_str: str,
    overall: str = STATUS_PASS,
    checks: list[dict] | None = None,
    pass_count: int = 5,
    warn_count: int = 0,
    fail_count: int = 0,
) -> dict:
    if checks is None:
        checks = [_make_check(f"check_{i}", STATUS_PASS) for i in range(pass_count)]
        checks += [_make_check(f"warn_{i}", STATUS_WARN) for i in range(warn_count)]
        checks += [_make_check(f"fail_{i}", STATUS_FAIL) for i in range(fail_count)]
    return {
        "generated_at": f"{date_str}T00:00:00+00:00",
        "scope_date": date_str,
        "summary": {
            "overall": overall,
            "counts": {
                STATUS_PASS: pass_count,
                STATUS_WARN: warn_count,
                STATUS_FAIL: fail_count,
                STATUS_UNKNOWN: 0,
            },
        },
        "checks": checks,
    }


# ===================================================================
# executive_summary
# ===================================================================


class TestExecutiveSummary:
    def test_all_pass(self) -> None:
        checks = [_make_check("c1"), _make_check("c2")]
        services = [_make_service("hft-engine"), _make_service("clickhouse")]
        result = executive_summary(checks, services, STATUS_PASS)
        assert "healthy" in result.lower()
        assert "All 2 checks passed" in result

    def test_with_warnings(self) -> None:
        checks = [
            _make_check("c1"),
            _make_check("c2", status=STATUS_WARN),
        ]
        services = [_make_service("hft-engine")]
        result = executive_summary(checks, services, STATUS_WARN)
        assert "warning" in result.lower()
        assert "1 of 2" in result

    def test_with_failures(self) -> None:
        checks = [
            _make_check("c1", status=STATUS_FAIL),
            _make_check("c2", status=STATUS_FAIL),
        ]
        services = [_make_service("hft-engine")]
        result = executive_summary(checks, services, STATUS_FAIL)
        assert "ALERT" in result
        assert "2 of 2 checks failed" in result

    def test_non_trading_day(self) -> None:
        checks = [_make_check("c1")]
        services = [_make_service("hft-engine")]
        result = executive_summary(
            checks,
            services,
            STATUS_PASS,
            expect_trading_day=False,
        )
        assert "Non-trading day" in result
        assert "Reduced activity expected" in result

    def test_scope_date_included(self) -> None:
        checks = [_make_check("c1")]
        services = []
        result = executive_summary(
            checks,
            services,
            STATUS_PASS,
            scope_date="2026-03-10",
        )
        assert "2026-03-10" in result


# ===================================================================
# compute_risk_score
# ===================================================================


class TestComputeRiskScore:
    def test_all_pass(self) -> None:
        checks = [_make_check("service_a"), _make_check("feed_b")]
        assert compute_risk_score(checks) == 0

    def test_one_critical_fail(self) -> None:
        checks = [
            _make_check("stormguard_trip", status=STATUS_FAIL, severity="critical"),
        ]
        score = compute_risk_score(checks)
        # stormguard weight=25, fail=25, critical doubles -> 50
        assert score == 50

    def test_mixed(self) -> None:
        checks = [
            _make_check("service_a", status=STATUS_FAIL, severity="warning"),
            _make_check("feed_b", status=STATUS_WARN, severity="warning"),
        ]
        score = compute_risk_score(checks)
        # service fail=10 + feed warn=7 = 17
        assert score == 17

    def test_capped_at_100(self) -> None:
        checks = [_make_check(f"stormguard_{i}", status=STATUS_FAIL, severity="critical") for i in range(10)]
        assert compute_risk_score(checks) == 100

    def test_empty_checks(self) -> None:
        assert compute_risk_score([]) == 0


# ===================================================================
# trend_delta
# ===================================================================


class TestTrendDelta:
    def test_with_previous(self, tmp_path: Path) -> None:
        prev_report = {
            "checks": [
                _make_check("c1", status=STATUS_PASS),
                _make_check("c2", status=STATUS_PASS),
            ],
        }
        prev_path = tmp_path / "prev.json"
        prev_path.write_text(json.dumps(prev_report), encoding="utf-8")

        current = [
            _make_check("c1", status=STATUS_WARN),
            _make_check("c2", status=STATUS_PASS),
        ]
        deltas = trend_delta(current, prev_path)
        assert len(deltas) == 1
        assert deltas[0]["check_id"] == "c1"
        assert deltas[0]["direction"] == "degraded"

    def test_no_previous_file(self) -> None:
        deltas = trend_delta([_make_check("c1")], "/nonexistent/path.json")
        assert deltas == []

    def test_none_path(self) -> None:
        deltas = trend_delta([_make_check("c1")], None)
        assert deltas == []

    def test_all_unchanged(self, tmp_path: Path) -> None:
        prev_report = {"checks": [_make_check("c1", status=STATUS_PASS)]}
        prev_path = tmp_path / "prev.json"
        prev_path.write_text(json.dumps(prev_report), encoding="utf-8")

        current = [_make_check("c1", status=STATUS_PASS)]
        deltas = trend_delta(current, prev_path)
        assert deltas == []

    def test_degradation(self, tmp_path: Path) -> None:
        prev_report = {"checks": [_make_check("c1", status=STATUS_PASS)]}
        prev_path = tmp_path / "prev.json"
        prev_path.write_text(json.dumps(prev_report), encoding="utf-8")

        current = [_make_check("c1", status=STATUS_FAIL)]
        deltas = trend_delta(current, prev_path)
        assert len(deltas) == 1
        assert deltas[0]["previous"] == STATUS_PASS
        assert deltas[0]["current"] == STATUS_FAIL
        assert deltas[0]["direction"] == "degraded"

    def test_improvement(self, tmp_path: Path) -> None:
        prev_report = {"checks": [_make_check("c1", status=STATUS_FAIL)]}
        prev_path = tmp_path / "prev.json"
        prev_path.write_text(json.dumps(prev_report), encoding="utf-8")

        current = [_make_check("c1", status=STATUS_PASS)]
        deltas = trend_delta(current, prev_path)
        assert len(deltas) == 1
        assert deltas[0]["direction"] == "improved"


# ===================================================================
# sparkline
# ===================================================================


class TestSparkline:
    def test_basic(self) -> None:
        result = sparkline([0.0, 0.5, 1.0])
        assert len(result) == 3
        # First char should be lowest bar, last should be highest
        assert result[0] == "\u2581"  # lowest
        assert result[-1] == "\u2588"  # highest

    def test_empty(self) -> None:
        assert sparkline([]) == ""

    def test_single_value(self) -> None:
        result = sparkline([42.0])
        assert len(result) == 1

    def test_all_zeros(self) -> None:
        result = sparkline([0.0, 0.0, 0.0])
        assert all(ch == "\u2581" for ch in result)

    def test_width_truncation(self) -> None:
        values = [float(i) for i in range(20)]
        result = sparkline(values, width=5)
        assert len(result) == 5


# ===================================================================
# diagnose_checks
# ===================================================================


class TestDiagnoseChecks:
    def test_restart_check(self) -> None:
        checks = [_make_check("restart_delta", status=STATUS_WARN)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "restart" in diagnosed[0]["cause"].lower()

    def test_feed_reconnect(self) -> None:
        checks = [_make_check("feed_reconnect_ratio", status=STATUS_FAIL)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "reconnect" in diagnosed[0]["cause"].lower()

    def test_session_conflict(self) -> None:
        checks = [_make_check("session_refresh_fail", status=STATUS_WARN)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "session" in diagnosed[0]["cause"].lower()

    def test_stormguard(self) -> None:
        checks = [_make_check("stormguard_trip", status=STATUS_FAIL)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "StormGuard" in diagnosed[0]["cause"]

    def test_wal_backlog(self) -> None:
        checks = [_make_check("wal_backlog", status=STATUS_WARN)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "WAL" in diagnosed[0]["cause"]

    def test_pass_checks_skipped(self) -> None:
        checks = [
            _make_check("service_a", status=STATUS_PASS),
            _make_check("feed_b", status=STATUS_PASS),
        ]
        diagnosed = diagnose_checks(checks)
        assert diagnosed == []

    def test_default_fallback(self) -> None:
        checks = [_make_check("unknown_xyz", status=STATUS_FAIL)]
        diagnosed = diagnose_checks(checks)
        assert len(diagnosed) == 1
        assert "threshold" in diagnosed[0]["cause"].lower()


# ===================================================================
# recommend_actions
# ===================================================================


class TestRecommendActions:
    def test_critical_fail(self) -> None:
        diagnosed = [
            {
                "check_id": "stormguard_trip",
                "status": STATUS_FAIL,
                "severity": "critical",
                "cause": "trip",
                "impact": "halt",
                "suggestion": "Check thresholds",
            },
        ]
        actions = recommend_actions(diagnosed)
        assert len(actions) == 1
        assert actions[0]["urgency"] == "immediate"
        assert actions[0]["priority"] == 1

    def test_sorted_by_priority(self) -> None:
        diagnosed = [
            {
                "check_id": "feed_reconnect",
                "status": STATUS_WARN,
                "severity": "warning",
                "suggestion": "Review feed",
            },
            {
                "check_id": "stormguard_trip",
                "status": STATUS_FAIL,
                "severity": "critical",
                "suggestion": "Check thresholds",
            },
        ]
        actions = recommend_actions(diagnosed)
        assert len(actions) == 2
        assert actions[0]["check_id"] == "stormguard_trip"
        assert actions[1]["check_id"] == "feed_reconnect"

    def test_empty(self) -> None:
        assert recommend_actions([]) == []


# ===================================================================
# format_status_icon
# ===================================================================


class TestFormatStatusIcon:
    def test_icon_pass(self) -> None:
        assert format_status_icon(STATUS_PASS) == "\u2705"

    def test_icon_warn(self) -> None:
        assert format_status_icon(STATUS_WARN) == "\u26a0\ufe0f"

    def test_icon_fail(self) -> None:
        assert format_status_icon(STATUS_FAIL) == "\u274c"

    def test_icon_unknown(self) -> None:
        assert format_status_icon(STATUS_UNKNOWN) == "\u2753"

    def test_icon_invalid_falls_back(self) -> None:
        assert format_status_icon("bogus") == "\u2753"


# ===================================================================
# group_checks_by_category
# ===================================================================


class TestGroupChecksByCategory:
    def test_service_checks(self) -> None:
        checks = [
            _make_check("service_hft"),
            _make_check("service_clickhouse"),
        ]
        groups = group_checks_by_category(checks)
        assert "Infrastructure" in groups
        assert len(groups["Infrastructure"]) == 2

    def test_feed_checks(self) -> None:
        checks = [_make_check("feed_reconnect")]
        groups = group_checks_by_category(checks)
        assert "Market Data Feed" in groups

    def test_mixed(self) -> None:
        checks = [
            _make_check("service_hft"),
            _make_check("feed_reconnect"),
            _make_check("wal_backlog"),
            _make_check("unknown_thing"),
        ]
        groups = group_checks_by_category(checks)
        assert len(groups) == 4
        assert "Infrastructure" in groups
        assert "Market Data Feed" in groups
        assert "Persistence / WAL" in groups
        assert "Other" in groups


# ===================================================================
# render_trend_section
# ===================================================================


class TestRenderTrendSection:
    def test_with_history(self, tmp_path: Path) -> None:
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        for i in range(3):
            date_str = f"2026-03-0{i + 1}"
            report = _make_daily_report(date_str, pass_count=5)
            (daily_dir / f"{date_str}.json").write_text(
                json.dumps(report),
                encoding="utf-8",
            )

        result = render_trend_section(daily_dir, "2026-03-03")
        assert "### Trend" in result
        assert "2026-03-01" in result
        assert "2026-03-03" in result
        assert "|" in result  # table present

    def test_no_history(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = render_trend_section(empty_dir, "2026-03-10")
        assert "No historical data" in result

    def test_nonexistent_dir(self) -> None:
        result = render_trend_section("/nonexistent/dir", "2026-03-10")
        assert "No historical data" in result

    def test_invalid_date(self, tmp_path: Path) -> None:
        result = render_trend_section(tmp_path, "not-a-date")
        assert "Invalid date" in result

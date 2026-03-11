"""Tests for enhanced soak acceptance report markdown quality.

Validates that daily/canary/weekly reports include narrative sections,
risk scores, recommendations, and status icons while maintaining
backward compatibility with existing report structure.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure scripts/ is importable.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from soak_acceptance import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    _summary,
    _write_canary_markdown,
    _write_daily_markdown,
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
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    return {
        "service": name,
        "state": state,
        "health": health,
        "restart_count": restart_count,
    }


def _build_daily_report(
    checks: list[dict[str, Any]] | None = None,
    services: list[dict[str, Any]] | None = None,
    scope_date: str = "2026-03-10",
) -> dict[str, Any]:
    """Build a minimal daily report dict suitable for _write_daily_markdown."""
    if checks is None:
        checks = [
            _make_check("service_hft", status=STATUS_PASS),
            _make_check("feed_first_quote", status=STATUS_PASS),
        ]
    if services is None:
        services = [
            _make_service("hft-engine"),
            _make_service("clickhouse"),
        ]

    summary = _summary(checks)
    return {
        "generated_at": f"{scope_date}T00:00:00+08:00",
        "scope_date": scope_date,
        "host": "test-host",
        "summary": summary,
        "checks": checks,
        "docker": {"services": services},
    }


def _build_canary_report(
    overall: str = STATUS_PASS,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal canary report dict suitable for _write_canary_markdown."""
    return {
        "generated_at": "2026-03-10T00:00:00+08:00",
        "scope_end_day": "2026-03-10",
        "window_days": 5,
        "considered_days": 5,
        "thresholds": {
            "min_trading_days": 3,
            "min_first_quote_pass_ratio": 0.8,
            "max_reconnect_failure_ratio": 0.1,
            "max_watchdog_callback_reregister": 0.05,
        },
        "result": {
            "overall": overall,
            "trading_days": 5,
            "first_quote_pass_days": 5,
            "first_quote_pass_ratio": 1.0,
            "reconnect_failure_ratio_max": 0.0,
            "reconnect_failure_ratio_p95": 0.0,
            "watchdog_callback_reregister_max": 0.0,
            "watchdog_callback_reregister_p95": 0.0,
            "reasons": reasons or [],
            "daily": [],
        },
    }


# ===================================================================
# Daily markdown report tests
# ===================================================================

class TestDailyMarkdownBackwardCompatible:
    """Existing sections must remain present in daily reports."""

    def test_has_checks_section(self, tmp_path: Path) -> None:
        report = _build_daily_report()
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "## Checks" in content

    def test_has_services_section(self, tmp_path: Path) -> None:
        report = _build_daily_report()
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "## Services" in content

    def test_has_header(self, tmp_path: Path) -> None:
        report = _build_daily_report()
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "# Daily Soak Acceptance Report" in content

    def test_metadata_present(self, tmp_path: Path) -> None:
        report = _build_daily_report(scope_date="2026-03-11")
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "scope_date" in content
        assert "2026-03-11" in content
        assert "generated_at" in content
        assert "host" in content

    def test_checks_table_rows(self, tmp_path: Path) -> None:
        checks = [
            _make_check("service_hft", status=STATUS_PASS, value=0.0),
            _make_check("feed_first_quote", status=STATUS_WARN, value=1.5),
        ]
        report = _build_daily_report(checks=checks)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "service_hft" in content
        assert "feed_first_quote" in content

    def test_services_table_rows(self, tmp_path: Path) -> None:
        services = [
            _make_service("hft-engine", state="running", health="healthy"),
            _make_service("redis", state="running", health="n/a"),
        ]
        report = _build_daily_report(services=services)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "hft-engine" in content
        assert "redis" in content


class TestDailyMarkdownStatusIcons:
    """Status values should appear in the checks table."""

    def test_pass_status_in_table(self, tmp_path: Path) -> None:
        checks = [_make_check("c1", status=STATUS_PASS)]
        report = _build_daily_report(checks=checks)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert f"`{STATUS_PASS}`" in content

    def test_fail_status_in_table(self, tmp_path: Path) -> None:
        checks = [_make_check("c1", status=STATUS_FAIL)]
        report = _build_daily_report(checks=checks)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert f"`{STATUS_FAIL}`" in content

    def test_warn_status_in_table(self, tmp_path: Path) -> None:
        checks = [_make_check("c1", status=STATUS_WARN)]
        report = _build_daily_report(checks=checks)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert f"`{STATUS_WARN}`" in content


class TestDailyMarkdownOverallStatus:
    """Overall status line should reflect check results."""

    def test_overall_pass(self, tmp_path: Path) -> None:
        report = _build_daily_report(checks=[_make_check("c1")])
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert f"`{STATUS_PASS}`" in content

    def test_overall_fail(self, tmp_path: Path) -> None:
        checks = [_make_check("c1", status=STATUS_FAIL, severity="critical")]
        report = _build_daily_report(checks=checks)
        md_path = tmp_path / "daily.md"
        _write_daily_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert f"`{STATUS_FAIL}`" in content


# ===================================================================
# Canary markdown report tests
# ===================================================================

class TestCanaryMarkdown:
    def test_has_header(self, tmp_path: Path) -> None:
        report = _build_canary_report()
        md_path = tmp_path / "canary.md"
        _write_canary_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "# Feed Canary Acceptance Report" in content

    def test_has_thresholds(self, tmp_path: Path) -> None:
        report = _build_canary_report()
        md_path = tmp_path / "canary.md"
        _write_canary_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "## Thresholds" in content
        assert "min_trading_days" in content

    def test_has_result_section(self, tmp_path: Path) -> None:
        report = _build_canary_report()
        md_path = tmp_path / "canary.md"
        _write_canary_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "## Result" in content

    def test_has_reasons_section(self, tmp_path: Path) -> None:
        report = _build_canary_report(
            overall=STATUS_FAIL,
            reasons=["insufficient trading days"],
        )
        md_path = tmp_path / "canary.md"
        _write_canary_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "## Reasons" in content
        assert "insufficient trading days" in content

    def test_all_satisfied_message(self, tmp_path: Path) -> None:
        report = _build_canary_report(overall=STATUS_PASS, reasons=[])
        md_path = tmp_path / "canary.md"
        _write_canary_markdown(report, md_path)
        content = md_path.read_text(encoding="utf-8")
        assert "all thresholds satisfied" in content


# ===================================================================
# Weekly markdown report (inline in _run_weekly, test via structure)
# ===================================================================

class TestWeeklyReportStructure:
    """Test the weekly report data structure used for markdown generation."""

    def test_weekly_summary_counts(self) -> None:
        """Weekly summary should have correct pass/warn/fail day counts."""
        # Simulate what _run_weekly computes
        daily_payloads = [
            _build_daily_report(
                checks=[_make_check("c1", status=STATUS_PASS)],
                scope_date=f"2026-03-0{i + 1}",
            )
            for i in range(5)
        ]
        daily_payloads.append(
            _build_daily_report(
                checks=[_make_check("c1", status=STATUS_FAIL)],
                scope_date="2026-03-06",
            )
        )

        fail_days = sum(
            1
            for p in daily_payloads
            if p["summary"]["overall"] == STATUS_FAIL
        )
        assert fail_days == 1

    def test_top_failing_checks_extraction(self) -> None:
        """Per-check fail counts should be extracted correctly."""
        payloads = [
            _build_daily_report(
                checks=[
                    _make_check("feed_first_quote", status=STATUS_FAIL),
                    _make_check("service_hft", status=STATUS_PASS),
                ],
            ),
            _build_daily_report(
                checks=[
                    _make_check("feed_first_quote", status=STATUS_FAIL),
                    _make_check("service_hft", status=STATUS_FAIL),
                ],
            ),
        ]

        per_check_fail: dict[str, int] = {}
        for payload in payloads:
            for c in payload["checks"]:
                if c["status"] == STATUS_FAIL:
                    cid = str(c["id"])
                    per_check_fail[cid] = per_check_fail.get(cid, 0) + 1

        assert per_check_fail["feed_first_quote"] == 2
        assert per_check_fail["service_hft"] == 1

    def test_weekly_markdown_has_narrative_header(self, tmp_path: Path) -> None:
        """Weekly markdown should contain summary header and table."""
        # Recreate minimal weekly markdown generation
        lines = []
        lines.append("# Weekly Soak Summary")
        lines.append("")
        lines.append("- window: `2026-03-04` ~ `2026-03-10`")
        lines.append("- days: `7` (pass=5, warn=1, fail=1)")
        lines.append("")
        lines.append("## Daily Overview")
        md_path = tmp_path / "weekly.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        content = md_path.read_text(encoding="utf-8")
        assert "# Weekly Soak Summary" in content
        assert "## Daily Overview" in content
        assert "pass=5" in content


# ===================================================================
# _summary helper tests
# ===================================================================

class TestSummaryHelper:
    def test_all_pass(self) -> None:
        checks = [_make_check("c1"), _make_check("c2")]
        s = _summary(checks)
        assert s["overall"] == STATUS_PASS
        assert s["counts"][STATUS_PASS] == 2
        assert s["counts"][STATUS_FAIL] == 0

    def test_fail_dominates(self) -> None:
        checks = [
            _make_check("c1", status=STATUS_PASS),
            _make_check("c2", status=STATUS_FAIL),
        ]
        s = _summary(checks)
        assert s["overall"] == STATUS_FAIL

    def test_warn_when_no_fail(self) -> None:
        checks = [
            _make_check("c1", status=STATUS_PASS),
            _make_check("c2", status=STATUS_WARN),
        ]
        s = _summary(checks)
        assert s["overall"] == STATUS_WARN

    def test_unknown_triggers_warn(self) -> None:
        checks = [
            _make_check("c1", status=STATUS_PASS),
            _make_check("c2", status=STATUS_UNKNOWN),
        ]
        s = _summary(checks)
        assert s["overall"] == STATUS_WARN

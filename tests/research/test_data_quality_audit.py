"""Tests for the raw-source data quality auditor.

Coverage gate is bypassed for this tree; invoke explicitly, e.g.:
    uv run pytest tests/research/test_data_quality_audit.py --no-cov -q
"""

from __future__ import annotations

import json
from pathlib import Path

from research.data_pipeline import quality

EIGHT_HOURS_NS = 8 * 3600 * 1_000_000_000


def _day(day: str, **overrides: int) -> quality.DayStats:
    base: dict[str, int | str] = {
        "day": day,
        "rows": 1_000_000,
        "symbols": 368,
        "causality_violations": 0,
        "max_skew_ns": 24_000_000,
        "outside_session": 5_000,
        "nonpositive_trade_price": 0,
        "negative_bid": 0,
        "crossed_book": 0,
        "ragged_depth": 0,
        "empty_book": 0,
        "duplicate_rows": 0,
        "first_exch_ts": 1,
        "last_exch_ts": 2,
    }
    base.update(overrides)
    return quality.DayStats(**base)  # type: ignore[arg-type]


def _clean_days(count: int = 5) -> list[quality.DayStats]:
    return [_day(f"2026-03-{index + 2:02d}") for index in range(count)]


def _report(days: list[quality.DayStats], **kwargs: object) -> quality.QualityReport:
    return quality.build_report(
        date_from=days[0].day,
        date_to=days[-1].day,
        days=days,
        months=[],
        trade_direction_present=False,
        **kwargs,  # type: ignore[arg-type]
    )


class TestCausality:
    def test_causality_check_flags_plus_8h_shifted_rows(self) -> None:
        """The regression this whole module exists for.

        Partitions 20260126-20260205 carried Taipei wall-clock written as UTC, so
        every row read exch_ts == ingest_ts + 8h. That is physically impossible and
        must surface as BROKEN, not as a warning.
        """
        shifted = [
            _day("2026-01-26", rows=1_087_967, causality_violations=1_087_967, max_skew_ns=EIGHT_HOURS_NS),
            _day("2026-01-27", rows=2_000_000, causality_violations=2_000_000, max_skew_ns=EIGHT_HOURS_NS),
        ]
        result = quality.evaluate_causality(shifted)

        assert result.status == "fail"
        assert result.severity == "error"
        assert result.detail["violating_rows"] == 3_087_967
        assert result.detail["violating_days"] == ["2026-01-26", "2026-01-27"]
        assert result.detail["max_skew_ns"] == EIGHT_HOURS_NS
        assert quality.classify_verdict([result]) == "BROKEN"

    def test_causality_passes_on_normal_broker_latency(self) -> None:
        result = quality.evaluate_causality(_clean_days())

        assert result.status == "pass"
        assert result.detail["violating_rows"] == 0

    def test_causality_is_unavailable_when_range_is_empty(self) -> None:
        result = quality.evaluate_causality([])

        assert result.status == "unavailable"
        assert quality.classify_verdict([result]) == "CLEAN"


class TestSessionWindow:
    def test_session_window_flags_day_with_most_rows_outside_sessions(self) -> None:
        days = [_day("2026-01-26", rows=1_000_000, outside_session=910_000)]

        result = quality.evaluate_session_window(days)

        assert result.status == "fail"
        assert result.detail["offending_days"][0]["outside_ratio"] == 0.91

    def test_session_window_tolerates_pre_open_auction_prints(self) -> None:
        days = [_day("2026-03-02", rows=1_000_000, outside_session=9_000)]

        result = quality.evaluate_session_window(days)

        assert result.status == "pass"


class TestVerdictPrecedence:
    def test_verdict_is_broken_when_an_error_check_fails(self) -> None:
        checks = [
            quality.CheckResult("ts_causality", "error", "fail", "boom"),
            quality.CheckResult("coverage_profile", "warn", "fail", "gaps"),
        ]

        assert quality.classify_verdict(checks) == "BROKEN"

    def test_verdict_is_degraded_when_only_warn_checks_fail(self) -> None:
        checks = [
            quality.CheckResult("ts_causality", "error", "pass", "ok"),
            quality.CheckResult("coverage_profile", "warn", "fail", "gaps"),
        ]

        assert quality.classify_verdict(checks) == "DEGRADED"

    def test_verdict_is_clean_when_info_checks_are_unavailable(self) -> None:
        checks = [
            quality.CheckResult("ts_causality", "error", "pass", "ok"),
            quality.CheckResult("eligibility", "info", "unavailable", "no mining stack"),
        ]

        assert quality.classify_verdict(checks) == "CLEAN"


class TestCoverage:
    def test_coverage_marks_expected_days_with_no_rows_as_missing(self) -> None:
        days = [_day("2026-03-02"), _day("2026-03-04")]
        expected = ["2026-03-02", "2026-03-03", "2026-03-04"]

        profile = quality.classify_coverage(days, expected_days=expected)

        assert [entry["status"] for entry in profile] == ["clean", "missing", "clean"]

    def test_coverage_marks_symbol_collapse_as_degraded(self) -> None:
        days = [*_clean_days(4), _day("2026-03-06", rows=35_585, symbols=57)]

        profile = quality.classify_coverage(days)

        assert profile[-1]["status"] == "degraded"

    def test_coverage_marks_low_row_day_as_partial(self) -> None:
        days = [*_clean_days(4), _day("2026-03-06", rows=200_000, symbols=350)]

        profile = quality.classify_coverage(days)

        assert profile[-1]["status"] == "partial"

    def test_coverage_labels_observed_non_session_dates_as_non_session(self) -> None:
        """A Friday night session's post-midnight rows land on Saturday under calendar-date grouping."""
        days = [*_clean_days(4), _day("2026-03-07", rows=900_000, symbols=360)]
        expected = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]

        profile = quality.classify_coverage(days, expected_days=expected)

        assert profile[-1] == {"day": "2026-03-07", "status": "non_session", "rows": 900_000, "symbols": 360}

    def test_non_session_days_do_not_make_coverage_fail(self) -> None:
        days = [*_clean_days(4), _day("2026-03-07", rows=900_000, symbols=360)]
        expected = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]

        result = quality.evaluate_coverage(days, expected_days=expected)

        assert result.status == "pass"
        assert result.detail["counts"]["non_session"] == 1

    def test_coverage_reports_expected_days_unknown_without_a_calendar(self) -> None:
        result = quality.evaluate_coverage(_clean_days(), expected_days=None)

        assert result.detail["expected_days_known"] is False


class TestUniverseDrift:
    def test_universe_drift_reports_large_pool_config_steps(self) -> None:
        days = [_day("2026-07-17", symbols=357), _day("2026-07-20", symbols=296)]

        result = quality.evaluate_universe_drift(days)

        assert result.status == "fail"
        assert result.detail["steps"][0]["delta"] == -61

    def test_universe_drift_cannot_see_a_three_percent_pool_change(self) -> None:
        """Documented limitation: a 368 -> 357 step is indistinguishable from rollover churn."""
        days = [_day("2026-07-16", symbols=368), _day("2026-07-17", symbols=357)]

        assert quality.evaluate_universe_drift(days).status == "pass"

    def test_universe_drift_ignores_single_symbol_churn(self) -> None:
        days = [_day("2026-07-17", symbols=357), _day("2026-07-20", symbols=356)]

        assert quality.evaluate_universe_drift(days).status == "pass"

    def test_universe_drift_ignores_proportionally_small_moves_on_a_large_universe(self) -> None:
        days = [_day("2026-05-18", symbols=523), _day("2026-05-19", symbols=505)]

        assert quality.evaluate_universe_drift(days).status == "pass"

    def test_universe_drift_skips_non_session_spillover_dates(self) -> None:
        """A Saturday holds only the Friday night-session tail; including it fakes two steps."""
        days = [
            _day("2026-03-06", symbols=98),
            _day("2026-03-07", symbols=48),  # Saturday spillover
            _day("2026-03-09", symbols=98),
        ]
        sessions = ["2026-03-06", "2026-03-09"]

        assert quality.evaluate_universe_drift(days).status == "fail"
        restricted = quality.evaluate_universe_drift(days, expected_days=sessions)
        assert restricted.status == "pass"
        assert restricted.detail["sessions_only"] is True


class TestFieldCoverage:
    def test_field_coverage_flags_months_without_trade_direction(self) -> None:
        months = [
            quality.MonthFieldStats(month=202602, rows=100, ticks=100, bidasks=0, snapshots=0, directed_ticks=0),
            quality.MonthFieldStats(month=202605, rows=100, ticks=100, bidasks=0, snapshots=0, directed_ticks=100),
        ]

        result = quality.evaluate_field_coverage(months, trade_direction_present=True)

        assert result.severity == "info"
        assert result.detail["months_below_full_direction_coverage"] == [202602]

    def test_field_coverage_is_unavailable_without_the_column(self) -> None:
        result = quality.evaluate_field_coverage([], trade_direction_present=False)

        assert result.status == "unavailable"


class TestEligibility:
    def test_eligibility_never_reports_a_self_computed_number(self) -> None:
        """The rule has exactly one authority; approximating it produced 92 and 33 where the truth was 60."""
        result = quality.evaluate_eligibility("2026-01-27", "2026-07-29")

        assert result.status == "unavailable"
        assert result.severity == "info"
        assert "taifex_trading_dates" in result.detail["authority"]


class TestReportFingerprint:
    def test_report_sha256_is_stable_under_key_reordering(self) -> None:
        first = quality.canonical_sha256({"a": 1, "b": {"c": 2, "d": 3}})
        second = quality.canonical_sha256({"b": {"d": 3, "c": 2}, "a": 1})

        assert first == second

    def test_report_sha256_changes_with_verdict(self) -> None:
        clean = _report(_clean_days())
        broken = _report([_day("2026-03-02", causality_violations=17)])

        assert broken.verdict == "BROKEN"
        assert clean.report_sha256 != broken.report_sha256

    def test_report_round_trips_through_its_payload(self) -> None:
        report = _report(_clean_days())

        restored = quality.QualityReport.from_payload(json.loads(json.dumps(report.to_payload())))

        assert restored.verdict == report.verdict
        assert restored.report_sha256 == report.report_sha256
        assert [c.check_id for c in restored.checks] == [c.check_id for c in report.checks]


class TestStamp:
    def test_stamp_is_unstamped_when_no_report_exists(self) -> None:
        stamp = quality.stamp_payload(None, requested_from="2026-03-02", requested_to="2026-03-02")

        assert stamp["source_quality_verdict"] == "unstamped"

    def test_stamp_is_unstamped_when_report_range_does_not_cover_dataset(self) -> None:
        report = _report(_clean_days())  # covers 2026-03-02 .. 2026-03-06

        stamp = quality.stamp_payload(report, requested_from="2026-03-02", requested_to="2026-07-29")

        assert stamp["source_quality_verdict"] == "unstamped_range_mismatch"
        assert stamp["source_quality_requested_range"] == ["2026-03-02", "2026-07-29"]
        assert stamp["source_quality_report_sha256"] == report.report_sha256

    def test_stamp_carries_verdict_and_findings_when_range_covers_dataset(self) -> None:
        report = _report([*_clean_days(4), _day("2026-03-06", rows=200_000, symbols=57)])

        stamp = quality.stamp_payload(report, requested_from="2026-03-03", requested_to="2026-03-05")

        assert stamp["source_quality_verdict"] == report.verdict
        assert any(finding.startswith("coverage_profile:") for finding in stamp["source_quality_findings"])

    def test_stamp_keys_are_all_source_quality_prefixed(self) -> None:
        report = _report(_clean_days())

        stamp = quality.stamp_payload(report, requested_from="2026-03-02", requested_to="2026-03-06")

        assert all(key.startswith("source_quality_") for key in stamp)


class TestCliExitCodes:
    """The audit is advisory by default; only --fail-on turns a verdict into an exit code."""

    @staticmethod
    def _run(monkeypatch: object, verdict: str, tmp_path: Path, *extra: str) -> int:
        import research.data_pipeline as pipeline

        days = [_day("2026-03-02")] if verdict == "CLEAN" else [_day("2026-03-02", causality_violations=1)]
        report = quality.build_report(
            date_from="2026-03-02",
            date_to="2026-03-02",
            days=days,
            months=[],
            trade_direction_present=False,
            expected_days=["2026-03-02"],
        )
        assert report.verdict == verdict
        monkeypatch.setattr(pipeline, "_get_client", lambda *a, **k: object())  # type: ignore[attr-defined]
        monkeypatch.setattr(pipeline.quality, "run_audit", lambda *a, **k: report)  # type: ignore[attr-defined]
        return pipeline.main(
            [
                "quality",
                "--date-from",
                "2026-03-02",
                "--date-to",
                "2026-03-02",
                "--out-dir",
                str(tmp_path),
                *extra,
            ]
        )

    def test_broken_verdict_exits_zero_without_fail_on(self, monkeypatch: object, tmp_path: Path) -> None:
        assert self._run(monkeypatch, "BROKEN", tmp_path) == 0

    def test_broken_verdict_exits_nonzero_with_fail_on_error(self, monkeypatch: object, tmp_path: Path) -> None:
        assert self._run(monkeypatch, "BROKEN", tmp_path, "--fail-on", "error") == 1

    def test_clean_verdict_exits_zero_with_fail_on_warn(self, monkeypatch: object, tmp_path: Path) -> None:
        assert self._run(monkeypatch, "CLEAN", tmp_path, "--fail-on", "warn") == 0


class TestReportPersistence:
    def test_written_report_is_loadable_and_latest_wins(self, tmp_path: Path) -> None:
        older = _report(_clean_days())
        newer = _report([_day("2026-03-02", causality_violations=1)])
        json_path, md_path = quality.write_report(older, tmp_path)
        # Force a distinct, later filename rather than relying on wall-clock spacing.
        (tmp_path / "29991231T235959_source_audit.json").write_text(
            json.dumps(newer.to_payload()), encoding="utf-8"
        )

        loaded = quality.load_latest_report(tmp_path)

        assert json_path.exists() and md_path.exists()
        assert loaded is not None
        assert loaded.report_sha256 == newer.report_sha256

    def test_load_latest_report_returns_none_for_missing_directory(self, tmp_path: Path) -> None:
        assert quality.load_latest_report(tmp_path / "does-not-exist") is None

    def test_markdown_report_includes_verdict_and_daily_coverage(self) -> None:
        report = _report([*_clean_days(4), _day("2026-03-06", rows=200_000, symbols=57)])

        rendered = quality.render_markdown(report)

        assert f"**{report.verdict}**" in rendered
        assert "## Daily coverage" in rendered
        assert "| 2026-03-06 | degraded |" in rendered

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


def _bucket(day: str, hour: int, rows: int, keys: int, kind: str = "Tick") -> quality.ContentBucket:
    return quality.ContentBucket(day=day, kind=kind, hour=hour, rows=rows, keys=keys)


def _day_buckets(day: str, multiplicity: float, *, hours: range = range(9, 14), rows: int = 100_000) -> list:
    return [_bucket(day, hour, rows, int(rows / multiplicity)) for hour in hours]


class TestContentDuplicates:
    def test_content_duplicates_flags_a_two_x_delivery_period(self) -> None:
        buckets = [
            *_day_buckets("2026-01-27", 2.0),
            *_day_buckets("2026-01-28", 2.0),
            *_day_buckets("2026-02-02", 1.02),
        ]

        result = quality.evaluate_content_duplicates(buckets)

        assert result.status == "fail"
        assert result.severity == "warn"
        assert result.detail["flagged_days"] == ["2026-01-27", "2026-01-28"]
        assert result.detail["heavy_days"] == ["2026-01-27", "2026-01-28"]
        assert result.detail["runs"] == [
            {"first_day": "2026-01-27", "last_day": "2026-01-28", "days": 2, "median_multiplicity": 2.0}
        ]

    def test_content_duplicates_reports_the_multiplicity_of_a_four_x_period(self) -> None:
        result = quality.evaluate_content_duplicates(_day_buckets("2026-05-15", 4.0))

        assert result.status == "fail"
        assert result.detail["runs"][0]["median_multiplicity"] == 4.0

    def test_content_duplicates_sees_a_mid_day_step_that_a_day_average_hides(self) -> None:
        """2026-06-02 jumped from 1x to 2x inside the 09:00 hour; the day mean is only ~1.6."""
        buckets = [
            *_day_buckets("2026-06-02", 1.0, hours=range(0, 9)),
            *_day_buckets("2026-06-02", 2.0, hours=range(9, 14)),
        ]

        result = quality.evaluate_content_duplicates(buckets)

        entry = result.detail["flagged"][0]
        assert result.status == "fail"
        assert entry["hours_flagged"] == 5
        assert entry["day_multiplicity"] < 1.6
        assert entry["worst_multiplicity"] == 2.0

    def test_content_duplicates_passes_on_clean_days(self) -> None:
        result = quality.evaluate_content_duplicates(
            [*_day_buckets("2026-09-15", 1.03), *_day_buckets("2026-09-16", 1.0)]
        )

        assert result.status == "pass"
        assert result.detail["flagged_days"] == []

    def test_content_duplicates_ignores_a_quiet_hour_that_is_repeated_on_every_clean_day(self) -> None:
        """The 14:00 after-hours session republishes frozen snapshots: ~1.9x on ~0.1% of a day."""
        buckets = [*_day_buckets("2026-09-15", 1.02, rows=1_000_000), _bucket("2026-09-15", 14, 15_961, 8_316)]

        result = quality.evaluate_content_duplicates(buckets)

        assert result.status == "pass"

    def test_content_duplicates_ignores_hours_with_too_few_rows(self) -> None:
        result = quality.evaluate_content_duplicates(
            [*_day_buckets("2026-09-15", 1.0), _bucket("2026-09-15", 4, 200, 50)]
        )

        assert result.status == "pass"

    def test_content_duplicates_is_unavailable_when_not_collected_or_empty(self) -> None:
        assert quality.evaluate_content_duplicates(None).status == "unavailable"
        assert quality.evaluate_content_duplicates([]).status == "unavailable"

    def test_content_key_excludes_the_fields_that_differ_between_copies(self) -> None:
        """The whole point of this check: copies differ only in ingest_ts and seq_no."""
        hashed = quality.CONTENT_DUP_QUERY.split("cityHash64(")[1].split(")) AS keys")[0]

        assert "ingest_ts" not in hashed
        assert "seq_no" not in hashed
        assert "exch_ts" in hashed
        assert "price_scaled" in hashed

    def test_duplicate_keys_names_its_blind_spot(self) -> None:
        result = quality.evaluate_duplicate_keys(_clean_days())

        assert "content_duplicates" in result.detail["blind_spot"]


def _clamp(day: str, group: str, ratio: float, rows: int = 1_000_000) -> quality.ClampStats:
    return quality.ClampStats(day=day, group=group, rows=rows, clamped=int(rows * ratio))


class TestIngestClampRatio:
    def test_clamp_ratio_flags_days_with_most_arrival_times_clamped(self) -> None:
        stats = [
            _clamp("2026-04-14", "FUT", 0.61),
            _clamp("2026-04-15", "FUT", 0.35),
            _clamp("2026-05-04", "FUT", 0.01),
        ]

        result = quality.evaluate_ingest_clamp_ratio(stats)

        assert result.status == "fail"
        assert result.detail["groups"]["FUT"]["warn_days"] == 2
        assert result.detail["groups"]["FUT"]["severe_days"] == 1
        assert result.detail["severe_days"] == [{"day": "2026-04-14", "group": "FUT", "ratio": 0.61}]

    def test_clamp_ratio_reports_a_monthly_mean_per_group(self) -> None:
        stats = [_clamp("2026-04-14", "FUT", 0.6), _clamp("2026-04-15", "FUT", 0.4), _clamp("2026-05-04", "FUT", 0.0)]

        result = quality.evaluate_ingest_clamp_ratio(stats)

        assert result.detail["groups"]["FUT"]["monthly_mean"] == {"2026-04": 0.5, "2026-05": 0.0}

    def test_clamp_ratio_passes_when_the_clock_keeps_up(self) -> None:
        result = quality.evaluate_ingest_clamp_ratio(
            [_clamp("2026-05-04", "FUT", 0.01), _clamp("2026-05-04", "OTHER", 0.0)]
        )

        assert result.status == "pass"

    def test_clamp_ratio_ignores_groups_with_too_few_rows(self) -> None:
        result = quality.evaluate_ingest_clamp_ratio([_clamp("2026-04-14", "TXO", 0.9, rows=500)])

        assert result.status == "unavailable"

    def test_clamp_ratio_is_unavailable_when_not_collected(self) -> None:
        assert quality.evaluate_ingest_clamp_ratio(None).status == "unavailable"

    def test_clamp_ratio_is_invisible_to_the_causality_check(self) -> None:
        """Clamping guarantees ingest_ts >= exch_ts, so ts_causality cannot see it."""
        assert quality.evaluate_causality(_clean_days()).status == "pass"
        assert quality.evaluate_ingest_clamp_ratio([_clamp("2026-04-14", "FUT", 0.8)]).status == "fail"


def _series(
    symbol: str, max_gap: float, lost: float, *, day: str = "2026-05-11", session: str = "day", rows: int = 200_000
):
    return quality.GapSeries(
        day=day,
        symbol=symbol,
        session=session,
        rows=rows,
        max_gap_s=max_gap,
        lost_s=lost,
        max_gap_start_s=1_778_460_989,
    )


class TestIntraSessionGaps:
    def test_gaps_flag_a_near_month_series_with_a_long_silence(self) -> None:
        result = quality.evaluate_intra_session_gaps([_series("TXFE6", 250.0, 2776.0), _series("MXFE6", 2.0, 0.0)])

        assert result.status == "fail"
        assert result.detail["near_series_flagged"] == 1
        assert result.detail["worst"][0]["symbol"] == "TXFE6"

    def test_gaps_flag_cumulative_silence_even_when_no_single_gap_is_long(self) -> None:
        result = quality.evaluate_intra_session_gaps([_series("TXFE6", 40.0, 700.0)])

        assert result.status == "fail"

    def test_gaps_do_not_judge_an_illiquid_far_month_series(self) -> None:
        series = [_series("TXFE6", 2.0, 0.0, rows=300_000), _series("TXFH6", 175.0, 900.0, rows=25_000)]

        result = quality.evaluate_intra_session_gaps(series)

        assert result.status == "pass"
        assert result.detail["series_flagged_all"] == 1

    def test_gaps_report_an_outage_when_several_families_go_quiet_in_one_session(self) -> None:
        series = [_series("TXFF6", 3934.0, 5453.0), _series("MXFF6", 3930.0, 5323.0), _series("TMFF6", 3931.0, 5448.0)]

        result = quality.evaluate_intra_session_gaps(series)

        assert result.detail["outages"] == [
            {
                "day": "2026-05-11",
                "session": "day",
                "families": ["MXF", "TMF", "TXF"],
                "max_gap_s": 3934.0,
                "lost_s": 5453.0,
            }
        ]

    def test_gaps_do_not_call_one_quiet_family_an_outage(self) -> None:
        result = quality.evaluate_intra_session_gaps([_series("TMFF6", 120.0, 300.0), _series("TXFF6", 1.0, 0.0)])

        assert result.status == "fail"
        assert result.detail["outages"] == []

    def test_gaps_ignore_series_with_too_few_rows(self) -> None:
        assert quality.evaluate_intra_session_gaps([_series("TXFE6", 900.0, 900.0, rows=100)]).status == "unavailable"

    def test_gaps_are_unavailable_when_not_collected(self) -> None:
        assert quality.evaluate_intra_session_gaps(None).status == "unavailable"

    def test_gap_query_splits_a_night_session_at_midnight(self) -> None:
        """One series spanning both halves would report the 10 idle daytime hours as a gap."""
        assert "'night_pm'" in quality.GAP_QUERY
        assert "'night_am'" in quality.GAP_QUERY
        assert "'night'," not in quality.GAP_QUERY


class TestNonTradingDayRows:
    def test_non_trading_day_rows_flag_a_market_holiday_with_data(self) -> None:
        rows = [quality.ExchDateRows("2026-04-02", 6_205_298), quality.ExchDateRows("2026-04-03", 234_815)]

        result = quality.evaluate_non_trading_day_rows(rows, ["2026-04-01", "2026-04-02"])

        assert result.status == "fail"
        assert result.detail["offending_dates"] == [{"date": "2026-04-03", "rows": 234_815}]
        assert "2026-04-03" in result.summary

    def test_non_trading_day_rows_pass_when_every_date_is_a_session(self) -> None:
        rows = [quality.ExchDateRows("2026-04-01", 100), quality.ExchDateRows("2026-04-02", 100)]

        assert quality.evaluate_non_trading_day_rows(rows, ["2026-04-01", "2026-04-02"]).status == "pass"

    def test_non_trading_day_rows_ignore_close_time_stragglers(self) -> None:
        rows = [quality.ExchDateRows("2026-01-31", 8), quality.ExchDateRows("2026-03-28", 28)]

        result = quality.evaluate_non_trading_day_rows(rows, ["2026-04-01"])

        assert result.status == "pass"
        assert [item["date"] for item in result.detail["straggler_dates"]] == ["2026-01-31", "2026-03-28"]

    def test_non_trading_day_rows_are_unavailable_without_a_calendar(self) -> None:
        result = quality.evaluate_non_trading_day_rows([quality.ExchDateRows("2026-04-03", 5)], None)

        assert result.status == "unavailable"

    def test_non_trading_day_rows_are_unavailable_when_not_collected(self) -> None:
        assert quality.evaluate_non_trading_day_rows(None, ["2026-04-01"]).status == "unavailable"

    def test_non_trading_day_query_skips_the_night_session_tail(self) -> None:
        """A Friday night session legitimately runs to 05:00 Saturday; only later rows count."""
        assert f">= {quality.NON_TRADING_DAY_NIGHT_END_MINUTE}" in quality.EXCH_DATE_QUERY


def _expiry(day: str, expiry: str, lo: float, hi: float, rows: int = 500_000) -> quality.ChainExpiry:
    return quality.ChainExpiry(day=day, expiry=expiry, min_strike=lo, max_strike=hi, strikes=50, rows=rows)


class TestOptionChainAtmCoverage:
    def test_atm_coverage_flags_a_chain_that_stopped_below_the_futures_price(self) -> None:
        """2026-09-15: F = 45,754 but the near-month chain tops out at 44,200."""
        chain = [_expiry("2026-09-15", "2026-09", 39_000, 44_200), _expiry("2026-09-15", "2026-10", 40_900, 46_100)]

        result = quality.evaluate_option_chain_atm_coverage(
            chain, [quality.FuturesRef("2026-09-15", "TXFI6", 45_754.0)]
        )

        assert result.status == "fail"
        assert result.detail["near_month_covered"] == 0
        assert result.detail["any_expiry_covered"] == 0
        assert result.detail["no_expiry_covered"] == 1

    def test_atm_coverage_counts_a_later_expiry_that_spans_the_price(self) -> None:
        chain = [_expiry("2026-09-15", "2026-09", 39_000, 44_200), _expiry("2026-09-15", "2026-10", 44_000, 47_000)]

        result = quality.evaluate_option_chain_atm_coverage(
            chain, [quality.FuturesRef("2026-09-15", "TXFI6", 45_754.0)]
        )

        assert result.status == "fail"
        assert result.detail["near_month_covered"] == 0
        assert result.detail["any_expiry_covered"] == 1

    def test_atm_coverage_passes_when_the_near_month_chain_spans_the_price(self) -> None:
        chain = [_expiry("2026-09-15", "2026-09", 43_000, 47_000)]

        result = quality.evaluate_option_chain_atm_coverage(
            chain, [quality.FuturesRef("2026-09-15", "TXFI6", 45_754.0)]
        )

        assert result.status == "pass"
        assert result.detail["by_month"] == {"2026-09": {"days": 1, "near_covered": 1, "any_covered": 1}}

    def test_atm_coverage_ignores_a_thinly_recorded_expiry(self) -> None:
        chain = [
            _expiry("2026-09-15", "2026-09", 30_000, 60_000, rows=100),
            _expiry("2026-09-15", "2026-10", 40_900, 46_100),
        ]

        result = quality.evaluate_option_chain_atm_coverage(
            chain, [quality.FuturesRef("2026-09-15", "TXFI6", 45_754.0)]
        )

        assert result.detail["any_expiry_covered"] == 0

    def test_atm_coverage_is_unavailable_without_a_futures_reference(self) -> None:
        chain = [_expiry("2026-09-15", "2026-09", 43_000, 47_000)]

        assert quality.evaluate_option_chain_atm_coverage(chain, []).status == "unavailable"
        assert quality.evaluate_option_chain_atm_coverage(None, None).status == "unavailable"

    def test_expiry_code_maps_call_and_put_letters_to_the_same_month(self) -> None:
        assert quality._expiry_from_code("C7") == "2027-03"
        assert quality._expiry_from_code("O7") == "2027-03"
        assert quality._expiry_from_code("B6") == "2026-02"
        assert quality._expiry_from_code("X6") == "2026-12"
        assert quality._expiry_from_code("ZZ") is None
        assert quality._expiry_from_code("C") is None


class _FakeResult:
    def __init__(self, rows: list[list[object]]) -> None:
        self.result_rows = rows


class _FakeClient:
    """Answers each source-layer query with canned rows and records what was asked."""

    def __init__(self, answers: dict[str, list[list[object]]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def query(self, query: str, parameters: dict[str, str] | None = None, settings: object = None) -> _FakeResult:
        self.asked.append(query)
        return _FakeResult(self.answers.get(query, []))


class TestSourceFetchers:
    def test_exch_date_rows_are_summed_across_chunks_and_clipped_to_the_range(self) -> None:
        client = _FakeClient(
            {
                quality.EXCH_DATE_QUERY: [
                    ["2026-04-02", 10],
                    ["2026-04-03", 5],
                    ["2026-04-10", 99],
                    ["2026-03-31", 7],
                ]
            }
        )

        rows = quality.fetch_exch_date_rows(client, "2026-04-01", "2026-04-09", chunk_days=4)

        assert {item.date: item.rows for item in rows} == {"2026-04-02": 30, "2026-04-03": 15}
        assert len(client.asked) == 3

    def test_option_chain_merges_call_and_put_codes_and_skips_unparseable_ones(self) -> None:
        client = _FakeClient(
            {
                quality.CHAIN_QUERY: [
                    ["2026-09-15", "I6", 39_000, 44_200, 40, 100],
                    ["2026-09-15", "U6", 38_000, 43_800, 53, 200],
                    ["2026-09-15", "??", 1, 2, 1, 1],
                ]
            }
        )

        chain = quality.fetch_option_chain(client, "2026-09-15", "2026-09-15")

        assert chain == [
            quality.ChainExpiry(
                day="2026-09-15", expiry="2026-09", min_strike=38_000, max_strike=44_200, strikes=53, rows=300
            )
        ]

    def test_fetch_source_stats_collects_all_six_statistics(self) -> None:
        client = _FakeClient({})

        stats = quality.fetch_source_stats(client, "2026-09-15", "2026-09-15")

        assert quality.CONTENT_DUP_QUERY in client.asked
        assert quality.GAP_QUERY in client.asked
        assert quality.FUTURES_REF_QUERY in client.asked
        assert stats.content == [] and stats.clamp == [] and stats.gaps == []
        assert stats.exch_dates == [] and stats.chain == [] and stats.futures_ref == []

    def test_content_query_runs_one_day_at_a_time(self) -> None:
        client = _FakeClient({})

        quality.fetch_source_stats(client, "2026-09-15", "2026-09-17", chunk_days=4)

        assert client.asked.count(quality.CONTENT_DUP_QUERY) == 3


class TestSourceChecksInReport:
    def test_report_lists_the_source_checks_as_unavailable_when_not_collected(self) -> None:
        report = _report(_clean_days())
        by_id = {check.check_id: check for check in report.checks}

        for check_id in (
            "content_duplicates",
            "ingest_clamp_ratio",
            "intra_session_gaps",
            "non_trading_day_rows",
            "option_chain_atm_coverage",
        ):
            assert by_id[check_id].status == "unavailable"
        assert report.verdict == "CLEAN"

    def test_report_is_degraded_and_names_the_finding_when_content_is_duplicated(self) -> None:
        stats = quality.SourceStats(content=_day_buckets("2026-03-02", 2.0))

        report = _report(_clean_days(), source_stats=stats)

        assert report.verdict == "DEGRADED"
        assert any(finding.startswith("content_duplicates:") for finding in report.findings)

    def test_report_with_source_checks_round_trips_through_its_payload(self) -> None:
        stats = quality.SourceStats(content=_day_buckets("2026-03-02", 2.0))
        report = _report(_clean_days(), source_stats=stats)

        restored = quality.QualityReport.from_payload(json.loads(json.dumps(report.to_payload())))

        assert restored.report_sha256 == report.report_sha256
        assert restored.findings == report.findings

    def test_cli_flag_skips_the_deep_checks(self, monkeypatch: object, tmp_path: Path) -> None:
        import research.data_pipeline as pipeline

        seen: dict[str, object] = {}

        def fake_run_audit(*args: object, **kwargs: object) -> quality.QualityReport:
            seen.update(kwargs)
            return _report(_clean_days())

        monkeypatch.setattr(pipeline, "_get_client", lambda *a, **k: object())  # type: ignore[attr-defined]
        monkeypatch.setattr(pipeline.quality, "run_audit", fake_run_audit)  # type: ignore[attr-defined]
        argv = ["quality", "--date-from", "2026-03-02", "--date-to", "2026-03-06", "--out-dir", str(tmp_path)]

        assert pipeline.main([*argv, "--no-deep-checks"]) == 0
        assert seen["deep_checks"] is False
        assert pipeline.main(argv) == 0
        assert seen["deep_checks"] is True


class TestOfficialVolumeReconciliation:
    def _row(self, *, deduped: int, official: int = 60_000, raw: int | None = None, day: str = "2026-09-10") -> object:
        return quality.OfficialVolumeRow(
            day=day,
            product="TX",
            symbol="TXFJ6",
            raw=raw if raw is not None else deduped,
            deduped=deduped,
            official=official,
        )

    def test_a_complete_deduplicated_day_passes(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=59_700)])

        assert result.status == "pass"
        assert result.detail["rows"][0]["ratio"] == 0.995

    def test_missing_recording_fails_below_the_floor(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=50_000)])

        assert result.status == "fail"
        assert "TX 2026-09-10" in result.summary

    def test_duplicate_delivery_that_survives_is_caught_by_the_ceiling(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=119_000, raw=119_000)])

        assert result.status == "fail"
        assert result.detail["outside_band"][0]["ratio"] > quality.OFFICIAL_RATIO_MAX

    def test_the_raw_ratio_is_reported_next_to_the_deduplicated_one(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=59_700, raw=119_400)])

        assert result.detail["rows"][0]["raw_ratio"] == 1.99

    def test_days_with_a_thin_official_volume_are_not_judged(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=10, official=100)])

        assert result.status == "unavailable"

    def test_no_reference_is_unavailable_not_a_pass(self) -> None:
        assert quality.evaluate_official_volume_reconciliation(None).status == "unavailable"

    def test_a_day_with_no_recorded_ticks_is_listed_not_scored_as_a_mismatch(self) -> None:
        rows = [self._row(deduped=59_700), self._row(deduped=0, day="2026-10-05")]

        result = quality.evaluate_official_volume_reconciliation(rows)

        assert result.status == "pass"
        assert result.detail["no_local_rows"] == ["2026-10-05"]

    def test_only_uncollected_days_leave_the_check_unavailable(self) -> None:
        result = quality.evaluate_official_volume_reconciliation([self._row(deduped=0, day="2026-10-05")])

        assert result.status == "unavailable"
        assert result.detail["no_local_rows"] == ["2026-10-05"]
        assert quality.evaluate_official_volume_reconciliation([]).status == "unavailable"


class _TickClient:
    """Answers every query with the same tick rows and records the parameters it was given."""

    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.parameters: list[dict[str, str]] = []

    def query(self, query: str, parameters: dict[str, str] | None = None, settings: object = None) -> _FakeResult:
        self.parameters.append(dict(parameters or {}))
        return _FakeResult(self.rows)


class TestOfficialVolumeFetch:
    SUMMARY = [
        {"product": "TX", "expiry": "202610", "session": "day", "spread": False, "contracts": 41_765, "trades": 9},
        {"product": "TX", "expiry": "202611", "session": "day", "spread": False, "contracts": 333, "trades": 9},
        {"product": "TX", "expiry": "202610/202611", "session": "day", "spread": True, "contracts": 180, "trades": 9},
        {"product": "TX", "expiry": "202610", "session": "night", "spread": False, "contracts": 32_209, "trades": 9},
        {"product": "TXO", "expiry": "202610", "session": "day", "spread": False, "contracts": 999, "trades": 1},
    ]

    def _root(self, tmp_path: Path) -> Path:
        folder = tmp_path / "parsed" / "fut_ticks"
        folder.mkdir(parents=True)
        (folder / "2026-10-05.summary.json").write_text(json.dumps(self.SUMMARY), encoding="utf-8")
        return tmp_path

    def test_the_near_month_is_the_busiest_day_session_single_leg_expiry(self, tmp_path: Path) -> None:
        volumes = quality.load_official_day_volumes(self._root(tmp_path), "2026-10-01", "2026-10-31")

        assert volumes == {"2026-10-05": {"TX": ("202610", 41_765)}}

    def test_days_outside_the_range_are_ignored(self, tmp_path: Path) -> None:
        assert quality.load_official_day_volumes(self._root(tmp_path), "2026-11-01", "2026-11-30") == {}

    def test_the_symbol_comes_from_product_and_expiry(self) -> None:
        assert quality._futures_symbol("TX", "202610") == "TXFJ6"
        assert quality._futures_symbol("MTX", "202602") == "MXFB6"
        assert quality._futures_symbol("TMF", "202612") == "TMFL6"
        assert quality._futures_symbol("TXO", "202610") is None
        assert quality._futures_symbol("TX", "2026") is None

    def test_without_a_root_the_statistic_is_not_collected(self) -> None:
        assert quality.fetch_official_volume(object(), "2026-10-01", "2026-10-31", None) is None

    def test_recorded_volume_is_deduplicated_and_compared(self, tmp_path: Path) -> None:
        ts = 1_790_000_000_000_000_000
        ticks = []
        for index in range(30):
            ticks += [(ts + index * 10**9, 21_000_000_000 + index, 2)] * 2  # every trade delivered twice
        client = _TickClient([[str(a), str(b), str(c)] for a, b, c in ticks])

        rows = quality.fetch_official_volume(client, "2026-10-01", "2026-10-31", self._root(tmp_path))

        assert rows == [
            quality.OfficialVolumeRow(
                day="2026-10-05", product="TX", symbol="TXFJ6", raw=120, deduped=60, official=41_765
            )
        ]
        assert client.parameters[0]["symbol"] == "TXFJ6"

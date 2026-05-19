from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.tools.t1_a_zero_event_diagnostic.cli import main
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def test_cli_emits_markdown_and_json(
    tmp_path: Path, make_coverage_csv, viability_event_csv
):
    rows = [
        coverage_row(
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
            trading_day="2026-04-01",
        ),
        coverage_row(
            break_side="up",
            break_magnitude_pts=10.0,
            max_upside_break_pts=12.0,
            max_downside_break_pts=0.0,
            realized_vol_ratio=1.4,
            vwap_side_at_break="above",
            event_selected_by_v0=True,
            trading_day="2026-04-02",
        ),
    ]
    cov = make_coverage_csv(rows)
    ev = viability_event_csv(n_events=0)
    md = tmp_path / "out.md"
    js = tmp_path / "out.json"
    rc = main(
        [
            "--coverage-csv",
            str(cov),
            "--viability-events-csv",
            str(ev),
            "--out-markdown",
            str(md),
            "--out-json",
            str(js),
        ]
    )
    assert rc == 0
    assert md.exists() and js.exists()
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["verdict"] == "DETECTOR_BUG"
    assert payload["primary_reason"] == "A5"
    assert "run_config" in payload
    assert "coverage_csv_sha256_by_path" in payload["run_config"]
    assert "spec_sha256" in payload["run_config"]
    md_text = md.read_text(encoding="utf-8")
    assert "Verdict" in md_text
    assert "A5" in md_text


def test_cli_strict_json_no_nan(tmp_path: Path, make_coverage_csv, viability_event_csv):
    rows = [
        coverage_row(
            coverage_status="missing_opening",
            break_side="none",
            max_upside_break_pts=None,
            max_downside_break_pts=None,
            realized_vol_ratio=None,
            break_magnitude_vs_prior_realized_vol=None,
        )
    ]
    cov = make_coverage_csv(rows)
    ev = viability_event_csv(n_events=0)
    js = tmp_path / "out.json"
    rc = main(
        [
            "--coverage-csv",
            str(cov),
            "--viability-events-csv",
            str(ev),
            "--out-markdown",
            str(tmp_path / "out.md"),
            "--out-json",
            str(js),
        ]
    )
    assert rc == 0
    json.loads(js.read_text(encoding="utf-8"))
    raw = js.read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert "Infinity" not in raw


def test_cli_rejects_empty_coverage_input(
    tmp_path: Path, make_coverage_csv, viability_event_csv
):
    cov = make_coverage_csv([], name="empty.csv")
    ev = viability_event_csv(n_events=0)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--coverage-csv",
                str(cov),
                "--viability-events-csv",
                str(ev),
                "--out-markdown",
                str(tmp_path / "x.md"),
                "--out-json",
                str(tmp_path / "x.json"),
            ]
        )
    assert exc.value.code != 0


def test_cli_concatenates_multiple_csv_inputs(
    tmp_path: Path, make_coverage_csv, viability_event_csv
):
    a = make_coverage_csv([coverage_row(trading_day="2026-04-01")], name="a.csv")
    b = make_coverage_csv([coverage_row(trading_day="2026-04-02")], name="b.csv")
    ev = viability_event_csv(n_events=0)
    rc = main(
        [
            "--coverage-csv",
            str(a),
            "--coverage-csv",
            str(b),
            "--viability-events-csv",
            str(ev),
            "--out-markdown",
            str(tmp_path / "out.md"),
            "--out-json",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 0
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["aggregate"]["n_total"] == 2


def test_cli_markdown_includes_contract_month_breakdown(
    tmp_path: Path, make_coverage_csv, viability_event_csv
):
    cov = make_coverage_csv([coverage_row(trading_day="2026-04-01")])
    ev = viability_event_csv(n_events=0)
    md = tmp_path / "out.md"
    main(
        [
            "--coverage-csv",
            str(cov),
            "--viability-events-csv",
            str(ev),
            "--out-markdown",
            str(md),
            "--out-json",
            str(tmp_path / "out.json"),
        ]
    )
    text = md.read_text(encoding="utf-8")
    assert "## Contract-Month Breakdown" in text
    assert "| TXFD6 | 2026-04 |" in text


def test_cli_reports_freshness_check(tmp_path: Path, make_coverage_csv):
    cov = make_coverage_csv([coverage_row(trading_day="2026-04-01")])
    ev = tmp_path / "20260513T153706Z_opening_range_events.csv"
    ev.write_text("", encoding="utf-8")
    summary = tmp_path / "20260513T153706Z_summary.json"
    summary.write_text('{"audited_trading_days": 1, "events": 0}', encoding="utf-8")
    js = tmp_path / "out.json"
    main(
        [
            "--coverage-csv",
            str(cov),
            "--viability-events-csv",
            str(ev),
            "--out-markdown",
            str(tmp_path / "out.md"),
            "--out-json",
            str(js),
        ]
    )
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["run_config"]["freshness_check"]["match"] is True
    assert payload["run_config"]["viability_summary_event_count"] == 0

"""Round 29: ``audit export`` CSV / Markdown emitter for goal §4 / §9."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    strategy: str = "demo",
    instrument: str = "TXFD6",
    strategy_type: str = "maker",
    profile: str = "vm_ul6_strict",
    edge: float | None,
    blocking_passed: bool | None = True,
    triage: str = "passed",
    spec_provenance: dict | None = None,
    ff_share: float | None = None,
    day_dom: float | None = None,
) -> None:
    advisory: list[dict] = []
    if edge is not None:
        advisory.append(
            {
                "name": "edge_per_round_trip",
                "passed": edge > 10.0,
                "metrics": {"mean_net_edge_pts_per_trade": edge},
                "details": "stub",
            }
        )
    if ff_share is not None:
        advisory.append(
            {
                "name": "force_flat_residual",
                "passed": ff_share <= 30.0,
                "metrics": {"force_flat_trip_share_pct": ff_share},
                "details": "stub",
            }
        )
    if day_dom is not None:
        advisory.append(
            {
                "name": "single_day_dominance",
                "passed": day_dom <= 25.0,
                "metrics": {"top_day_contribution_pct": day_dom, "threshold_pct": 25.0},
                "details": "stub",
            }
        )
    blocking: dict | None
    if blocking_passed is None:
        blocking = None
    else:
        blocking = {
            "passed": blocking_passed,
            "failing": [],
            "triage_status": triage,
        }
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=strategy,
        instrument=instrument,
        strategy_type=strategy_type,
        profile_name=profile,
        advisory=advisory,
        blocking=blocking,
        spec_provenance=spec_provenance,
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestAuditCliExport:
    def test_csv_round_trips_via_dictreader(self, _isolated) -> None:
        _record(
            run_id="e_a",
            strategy="alpha_one",
            edge=12.5,
            spec_provenance={
                "data_range": "2026-01..2026-03",
                "cost_model_id": "p95+0.4bp/2bp/0.5pts",
                "required_gates": [],
            },
        )
        _record(run_id="e_b", strategy="alpha_two", edge=6.5, blocking_passed=False, triage="killed")
        out = audit_cli.export(fmt="csv")
        reader = list(csv.DictReader(io.StringIO(out)))
        assert len(reader) == 2
        a = next(r for r in reader if r["run_id"] == "e_a")
        assert a["mean_net_edge_pts_per_trade"] == "12.500000"
        assert a["data_range"] == "2026-01..2026-03"
        assert a["blocking_passed"] == "true"
        b = next(r for r in reader if r["run_id"] == "e_b")
        assert b["triage_status"] == "killed"
        assert b["blocking_passed"] == "false"

    def test_markdown_table_has_header_and_separator(self, _isolated) -> None:
        _record(run_id="e_md", edge=11.0)
        out = audit_cli.export(fmt="md")
        lines = out.split("\n")
        assert lines[0].startswith("| run_id ")
        assert lines[1].startswith("|---")
        assert "e_md" in lines[2]
        assert "11.000000" in lines[2]

    def test_loose_row_blocking_renders_empty(self, _isolated) -> None:
        _record(run_id="e_loose", edge=12.0, blocking_passed=None)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["blocking_passed"] == ""

    def test_no_edge_row_renders_empty_edge(self, _isolated) -> None:
        _record(run_id="e_no_edge", edge=None)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["mean_net_edge_pts_per_trade"] == ""

    def test_edge_min_filter_applies(self, _isolated) -> None:
        _record(run_id="e_pass", edge=15.0)
        _record(run_id="e_drop", edge=8.0)
        out = audit_cli.export(fmt="csv", edge_min=10.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["e_pass"]

    def test_only_passing_filter_applies(self, _isolated) -> None:
        _record(run_id="e_p", edge=12.0, blocking_passed=True)
        _record(run_id="e_f", edge=12.0, blocking_passed=False, triage="killed")
        out = audit_cli.export(fmt="csv", only_passing=True)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["e_p"]

    def test_unsupported_format_raises(self, _isolated) -> None:
        with pytest.raises(ValueError):
            audit_cli.export(fmt="json")

    def test_markdown_escapes_pipe_in_cell(self, _isolated) -> None:
        _record(run_id="e_pipe", strategy="weird|name", edge=12.0)
        out = audit_cli.export(fmt="md")
        assert r"weird\|name" in out

    def test_main_dispatches_export_csv(self, _isolated, capsys) -> None:
        _record(run_id="e_main", edge=11.5)
        rc = audit_cli.main(["export", "--fmt", "csv"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "run_id,strategy_name" in captured
        assert "e_main" in captured


class TestAuditCliExportForceFlat:
    """Round 47: force_flat_trip_share_pct travels into exported artifacts."""

    def test_force_flat_column_in_csv_header(self, _isolated) -> None:
        _record(run_id="ff_hdr", edge=12.0, ff_share=15.0)
        out = audit_cli.export(fmt="csv")
        header = out.split("\n")[0]
        assert "force_flat_trip_share_pct" in header

    def test_force_flat_value_round_trips(self, _isolated) -> None:
        _record(run_id="ff_val", edge=12.0, ff_share=22.5)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["force_flat_trip_share_pct"] == "22.5000"

    def test_missing_force_flat_renders_empty(self, _isolated) -> None:
        _record(run_id="ff_none", edge=12.0, ff_share=None)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["force_flat_trip_share_pct"] == ""

    def test_max_force_flat_share_drops_over_bound(self, _isolated) -> None:
        _record(run_id="ff_keep", edge=12.0, ff_share=20.0)
        _record(run_id="ff_drop", edge=12.0, ff_share=55.0)
        out = audit_cli.export(fmt="csv", max_force_flat_share=30.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["ff_keep"]

    def test_max_force_flat_share_keeps_rows_without_metric(self, _isolated) -> None:
        # Gate didn't run -> no metric -> must not be silently dropped.
        _record(run_id="ff_no_metric", edge=12.0, ff_share=None)
        _record(run_id="ff_over", edge=12.0, ff_share=80.0)
        out = audit_cli.export(fmt="csv", max_force_flat_share=30.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["ff_no_metric"]

    def test_edge_min_and_max_force_flat_compose(self, _isolated) -> None:
        # Only the high-edge, low-force-flat candidate survives both filters.
        _record(run_id="clean", edge=15.0, ff_share=10.0)
        _record(run_id="low_edge", edge=5.0, ff_share=10.0)
        _record(run_id="propped", edge=15.0, ff_share=90.0)
        out = audit_cli.export(fmt="csv", edge_min=10.0, max_force_flat_share=30.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["clean"]

    def test_main_max_force_flat_flag(self, _isolated, capsys) -> None:
        _record(run_id="cli_keep", edge=12.0, ff_share=10.0)
        _record(run_id="cli_drop", edge=12.0, ff_share=70.0)
        rc = audit_cli.main(["export", "--fmt", "csv", "--max-force-flat-share", "30"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli_keep" in out
        assert "cli_drop" not in out


class TestAuditCliExportDayDominance:
    """Round 49: single_day_dominance_pct travels into exported artifacts."""

    def test_dominance_column_in_csv_header(self, _isolated) -> None:
        _record(run_id="dd_hdr", edge=12.0, day_dom=18.0)
        out = audit_cli.export(fmt="csv")
        assert "single_day_dominance_pct" in out.split("\n")[0]

    def test_dominance_value_round_trips(self, _isolated) -> None:
        _record(run_id="dd_val", edge=12.0, day_dom=17.5)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["single_day_dominance_pct"] == "17.5000"

    def test_missing_dominance_renders_empty(self, _isolated) -> None:
        _record(run_id="dd_none", edge=12.0, day_dom=None)
        out = audit_cli.export(fmt="csv")
        row = next(csv.DictReader(io.StringIO(out)))
        assert row["single_day_dominance_pct"] == ""

    def test_max_day_dominance_drops_over_bound(self, _isolated) -> None:
        _record(run_id="dd_keep", edge=12.0, day_dom=20.0)
        _record(run_id="dd_drop", edge=12.0, day_dom=60.0)
        out = audit_cli.export(fmt="csv", max_day_dominance=25.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["dd_keep"]

    def test_max_day_dominance_keeps_rows_without_metric(self, _isolated) -> None:
        _record(run_id="dd_no_metric", edge=12.0, day_dom=None)
        _record(run_id="dd_over", edge=12.0, day_dom=90.0)
        out = audit_cli.export(fmt="csv", max_day_dominance=25.0)
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["dd_no_metric"]

    def test_edge_force_flat_and_dominance_compose(self, _isolated) -> None:
        # Only the candidate clean on all three axes survives.
        _record(run_id="clean", edge=15.0, ff_share=10.0, day_dom=10.0)
        _record(run_id="propped_day", edge=15.0, ff_share=10.0, day_dom=90.0)
        _record(run_id="propped_ff", edge=15.0, ff_share=80.0, day_dom=10.0)
        out = audit_cli.export(
            fmt="csv", edge_min=10.0, max_force_flat_share=30.0, max_day_dominance=25.0
        )
        ids = [r["run_id"] for r in csv.DictReader(io.StringIO(out))]
        assert ids == ["clean"]

    def test_main_max_day_dominance_flag(self, _isolated, capsys) -> None:
        _record(run_id="cli_dd_keep", edge=12.0, day_dom=10.0)
        _record(run_id="cli_dd_drop", edge=12.0, day_dom=70.0)
        rc = audit_cli.main(["export", "--fmt", "csv", "--max-day-dominance", "25"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli_dd_keep" in out
        assert "cli_dd_drop" not in out

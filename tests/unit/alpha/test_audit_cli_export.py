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

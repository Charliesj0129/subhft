"""Round 58: spec-provenance completeness (goal §4 traceability — 資料區間 /
成本假設 / required-gate set). A row claiming promotability must carry an
intact audit trail; this is surfaced in `show()` and `export` separately from
the credibility verdict. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(*, run_id: str, prov: dict | None) -> None:
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
            "error": False,
        }
    ]
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
        spec_provenance=prov,
    )


_FULL = {
    "data_range": "2026-01-01..2026-03-31",
    "cost_model_id": "taifex_retail_v1",
    "required_gates": ["A", "B", "C"],
}


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestSpecProvenanceCompleteFunction:
    def test_full_provenance_is_complete(self, _isolated) -> None:
        _record(run_id="full", prov=dict(_FULL))
        ok, missing = audit_cli.spec_provenance_complete(_row("full"))
        assert ok is True
        assert missing == []

    def test_no_provenance_lists_all_keys(self, _isolated) -> None:
        _record(run_id="none", prov=None)
        ok, missing = audit_cli.spec_provenance_complete(_row("none"))
        assert ok is False
        assert set(missing) == {"data_range", "cost_model_id", "required_gates"}

    def test_empty_required_gates_is_incomplete(self, _isolated) -> None:
        prov = dict(_FULL)
        prov["required_gates"] = []
        _record(run_id="nogates", prov=prov)
        ok, missing = audit_cli.spec_provenance_complete(_row("nogates"))
        assert ok is False
        assert missing == ["required_gates"]

    def test_empty_cost_model_is_incomplete(self, _isolated) -> None:
        prov = dict(_FULL)
        prov["cost_model_id"] = ""
        _record(run_id="nocost", prov=prov)
        ok, missing = audit_cli.spec_provenance_complete(_row("nocost"))
        assert ok is False
        assert missing == ["cost_model_id"]


class TestShowSurfacesProvenanceCompleteness:
    def test_complete_line(self, _isolated) -> None:
        _record(run_id="s_ok", prov=dict(_FULL))
        out = audit_cli.show("s_ok")
        assert "spec_provenance: complete" in out

    def test_incomplete_lists_missing(self, _isolated) -> None:
        _record(run_id="s_bad", prov=None)
        out = audit_cli.show("s_bad")
        line = out.split("spec_provenance")[1].split("\n")[0]
        assert "INCOMPLETE" in line
        assert "data_range" in line


class TestSummaryAggregatesProvenance:
    def test_summary_counts_complete_and_top_missing(self, _isolated) -> None:
        _record(run_id="full", prov=dict(_FULL))
        _record(run_id="bare1", prov=None)
        _record(run_id="bare2", prov=None)
        out = audit_cli.summary()
        assert "spec_provenance (goal §4 traceability):" in out
        section = out.split("spec_provenance (goal")[1]
        assert "complete rows  : 1 / 3" in section
        # bare rows miss all three keys; ties resolve to a real missing key.
        assert "top missing key:" in section

    def test_summary_all_complete_has_no_top_missing(self, _isolated) -> None:
        _record(run_id="full", prov=dict(_FULL))
        out = audit_cli.summary()
        section = out.split("spec_provenance (goal")[1]
        assert "complete rows  : 1 / 1" in section
        assert "top missing key:" not in section.split("triage_status")[0]


class TestExportCarriesProvenanceCompleteness:
    def test_csv_column_true_false(self, _isolated) -> None:
        _record(run_id="full", prov=dict(_FULL))
        _record(run_id="bare", prov=None)
        out = audit_cli.export(fmt="csv")
        reader = csv.DictReader(io.StringIO(out))
        rows = {r["run_id"]: r for r in reader}
        assert "spec_provenance_complete" in reader.fieldnames
        assert rows["full"]["spec_provenance_complete"] == "true"
        assert rows["bare"]["spec_provenance_complete"] == "false"
